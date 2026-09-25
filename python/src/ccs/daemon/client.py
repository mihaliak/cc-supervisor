"""Clients for the daemon socket (ADR-0005: JSON Lines, `proto: 1`).

- `DaemonClient`: synchronous, for CLI commands (`ccs usage --refresh`, `ccs status`,
  `ccs events --follow`). Server pushes that arrive while waiting for a reply are buffered
  and returned by `iter_pushes()`.
- `AsyncDaemonClient`: asyncio, for long-lived clients (the P05 launcher, tests). Replies are
  matched by `id`; pushes (`event`, `snapshot`, `cmd`) go to `next_push()`.

Both refuse a socket they can't trust (`socket_trust_error`, ADR-0022) before connecting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from ccs import __version__, paths

PROTO = 1

log = logging.getLogger(__name__)


class DaemonUnavailable(Exception):
    """No daemon is listening, or it did not answer in time."""


def socket_trust_error(sock_path: Path) -> str | None:
    """Why the daemon socket must not be used, or None (ADR-0022).

    The socket (`lstat`: a symlink is refused) and its directory must belong to the current
    user, and the directory must not be group- or world-writable. A missing path is not an
    error here: connecting then fails as usual.
    """
    uid = os.getuid()
    try:
        link = os.lstat(sock_path.parent)
        folder = os.stat(sock_path.parent)  # a symlinked state dir is judged by its target
        sock = os.lstat(sock_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect: {exc}"
    if link.st_uid != uid or folder.st_uid != uid:
        return f"{sock_path.parent} is not owned by the current user"
    if folder.st_mode & 0o022:
        return f"{sock_path.parent} is group- or world-writable"
    if not stat.S_ISSOCK(sock.st_mode):
        return f"{sock_path} is not a socket"
    if sock.st_uid != uid:
        return f"{sock_path} is not owned by the current user"
    return None


def _check_trust(sock_path: Path) -> None:
    problem = socket_trust_error(sock_path)
    if problem is not None:
        log.warning("refusing the daemon socket: %s", problem)
        raise DaemonUnavailable(f"refusing the daemon socket: {problem}")


class DaemonClient:
    """One connection to `daemon.sock`. Use as a context manager."""

    def __init__(self, sock_path: Path | None = None, *, timeout: float = 2.0) -> None:
        self.sock_path = Path(sock_path) if sock_path is not None else paths.daemon_sock()
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0
        self._pushes: deque[dict[str, Any]] = deque()

    def connect(self) -> None:
        """Open the socket; raises `DaemonUnavailable`."""
        if self._sock is not None:
            return
        _check_trust(self.sock_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.sock_path))
        except OSError as exc:
            sock.close()
            raise DaemonUnavailable(f"daemon not reachable at {self.sock_path}: {exc}") from exc
        self._sock = sock

    def close(self) -> None:
        """Close the connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> DaemonClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _readline(self) -> bytes:
        assert self._sock is not None
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(65536)
            except OSError as exc:
                raise DaemonUnavailable(f"daemon read failed: {exc}") from exc
            if not chunk:
                raise DaemonUnavailable("daemon closed the connection")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def request(self, op: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return its reply (`ok` may be false)."""
        self.connect()
        assert self._sock is not None
        self._next_id += 1
        req_id = self._next_id
        msg = {"proto": PROTO, "id": req_id, "op": op, **fields}
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        except OSError as exc:
            raise DaemonUnavailable(f"daemon write failed: {exc}") from exc
        while True:
            line = self._readline()
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == req_id:
                return reply
            if isinstance(reply, dict) and "id" not in reply:
                self._pushes.append(reply)

    def hello(self, client: str = "cli") -> dict[str, Any]:
        """The `hello` handshake."""
        return self.request("hello", client=client, version=__version__)

    def subscribe(self, topics: list[str]) -> dict[str, Any]:
        """Subscribe to `events` and/or `snapshot` pushes."""
        return self.request("subscribe", topics=topics)

    def iter_pushes(self) -> Iterator[dict[str, Any]]:
        """Yield server pushes forever (blocks without a timeout). Ends when the daemon closes."""
        assert self._sock is not None
        self._sock.settimeout(None)
        while True:
            while self._pushes:
                yield self._pushes.popleft()
            try:
                line = self._readline()
            except DaemonUnavailable:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and "id" not in msg:
                self._pushes.append(msg)


def daemon_available(*, timeout: float = 1.0, sock_path: Path | None = None) -> bool:
    """True when a daemon answers `hello` on the socket."""
    try:
        with DaemonClient(sock_path, timeout=timeout) as client:
            return bool(client.hello().get("ok"))
    except DaemonUnavailable:
        return False


class AsyncDaemonClient:
    """An asyncio connection: `request()` awaits replies by id, pushes queue up separately."""

    def __init__(self, sock_path: Path | None = None, *, timeout: float = 5.0) -> None:
        self.sock_path = Path(sock_path) if sock_path is not None else paths.daemon_sock()
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._replies: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pushes: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.closed = False

    async def connect(self) -> None:
        """Open the connection; raises `DaemonUnavailable`."""
        _check_trust(self.sock_path)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.sock_path), limit=16 * 1024 * 1024),
                self.timeout,
            )
        except (OSError, TimeoutError) as exc:
            raise DaemonUnavailable(f"daemon not reachable at {self.sock_path}: {exc}") from exc
        self._task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                req_id = msg.get("id")
                fut = self._replies.pop(req_id, None) if isinstance(req_id, int) else None
                if fut is not None:
                    if not fut.done():
                        fut.set_result(msg)
                elif "id" not in msg or req_id is None:
                    self._pushes.put_nowait(msg)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            self.closed = True
            for fut in self._replies.values():
                if not fut.done():
                    fut.set_exception(DaemonUnavailable("daemon closed the connection"))
            self._replies.clear()
            self._pushes.put_nowait(None)

    async def send(self, obj: dict[str, Any]) -> None:
        """Write one raw message (e.g. a command ack)."""
        if self._writer is None or self.closed:
            raise DaemonUnavailable("not connected")
        try:
            self._writer.write((json.dumps({"proto": PROTO, **obj}) + "\n").encode("utf-8"))
            await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            raise DaemonUnavailable(f"daemon write failed: {exc}") from exc

    async def request(
        self, op: str, *, timeout: float | None = None, **fields: Any
    ) -> dict[str, Any]:
        """Send a request and await its reply (raises `DaemonUnavailable` on close/timeout)."""
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._replies[req_id] = fut
        await self.send({"id": req_id, "op": op, **fields})
        try:
            return await asyncio.wait_for(fut, timeout if timeout is not None else self.timeout)
        except TimeoutError as exc:
            self._replies.pop(req_id, None)
            raise DaemonUnavailable(f"no reply to {op}") from exc

    async def hello(self, client: str = "cli") -> dict[str, Any]:
        return await self.request("hello", client=client, version=__version__)

    async def next_push(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next pushed message (`None` once the connection closed)."""
        if timeout is None:
            return await self._pushes.get()
        return await asyncio.wait_for(self._pushes.get(), timeout)

    async def close(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.closed = True

    async def __aenter__(self) -> AsyncDaemonClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
