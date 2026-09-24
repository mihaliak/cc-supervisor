"""Synchronous client for the daemon socket (ADR-0005: JSON Lines, `proto: 1`).

Used by CLI commands (`ccs usage --refresh`, later `ccs status`, …). Server-pushed
`{"event": …}` / `{"cmd": …}` lines are skipped while waiting for a reply.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import TracebackType
from typing import Any

from ccs import __version__, paths

PROTO = 1


class DaemonUnavailable(Exception):
    """No daemon is listening, or it did not answer in time."""


class DaemonClient:
    """One connection to `daemon.sock`. Use as a context manager."""

    def __init__(self, sock_path: Path | None = None, *, timeout: float = 2.0) -> None:
        self.sock_path = Path(sock_path) if sock_path is not None else paths.daemon_sock()
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0

    def connect(self) -> None:
        """Open the socket; raises `DaemonUnavailable`."""
        if self._sock is not None:
            return
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

    def hello(self, client: str = "cli") -> dict[str, Any]:
        """The `hello` handshake."""
        return self.request("hello", client=client, version=__version__)


def daemon_available(*, timeout: float = 1.0, sock_path: Path | None = None) -> bool:
    """True when a daemon answers `hello` on the socket."""
    try:
        with DaemonClient(sock_path, timeout=timeout) as client:
            return bool(client.hello().get("ok"))
    except DaemonUnavailable:
        return False
