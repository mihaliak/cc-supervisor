"""The launcher's connection to the daemon (ADR-0005 IPC, ADR-0006 availability rules).

- Connect with a 1 s timeout. If the daemon is installed but not answering, start it like
  `ccs daemon start` (bootstrap if unloaded, else kickstart) and retry 3 times at 500 ms.
- Not installed → unsupervised with a one-line hint. Installed but unreachable →
  unsupervised for now, reconnecting in the background.
- After registration, pushes are read continuously; `{"cmd": …}` pushes run one at a time
  and are acked on the connection that delivered them.
- On EOF: back off 0.5/1/2/4/8/10 s, then re-`hello` and re-`register_wrapper` with the
  same `wrapper_id`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ccs.daemon.client import AsyncDaemonClient, DaemonUnavailable

log = logging.getLogger(__name__)

BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 10.0)
START_RETRIES_S = (0.5, 0.5, 0.5)

CmdHandler = Callable[[dict[str, Any]], Awaitable[tuple[str, dict[str, Any]]]]
SupervisionSink = Callable[[dict[str, Any]], None]
Printer = Callable[[str], None]


class LaunchdApi(Protocol):
    def is_installed(self) -> bool: ...

    def start(self) -> None: ...


class _DefaultLaunchd:
    def is_installed(self) -> bool:
        from ccs.daemon import launchd

        return launchd.is_installed()

    def start(self) -> None:
        from ccs.daemon import launchd

        launchd.start()


@dataclass(frozen=True)
class Registration:
    wrapper_id: str
    profile_id: str
    wrapper_pid: int
    claude_pid: int
    cwd: str
    started_overridden: bool


class DaemonLink:
    """State: `connected`, `retrying` (installed, unreachable) or `unsupervised`."""

    def __init__(
        self,
        profile_id: str,
        wrapper_id: str,
        *,
        sock_path: Path | None = None,
        launchd: LaunchdApi | None = None,
        err: Printer | None = None,
        connect_timeout: float = 1.0,
        start_retries_s: Sequence[float] = START_RETRIES_S,
        backoff_s: Sequence[float] = BACKOFF_S,
        request_timeout: float = 5.0,
    ) -> None:
        self.profile_id = profile_id
        self.wrapper_id = wrapper_id
        self.sock_path = sock_path
        self.launchd: LaunchdApi = launchd or _DefaultLaunchd()
        self.err: Printer = err or (lambda msg: None)
        self.connect_timeout = connect_timeout
        self.start_retries_s = tuple(start_retries_s)
        self.backoff_s = tuple(backoff_s) or (1.0,)
        self.request_timeout = request_timeout
        self.state = "unsupervised"
        self.client: AsyncDaemonClient | None = None
        self.registered = False
        self._reg: Registration | None = None
        self._on_cmd: CmdHandler | None = None
        self._on_supervision: SupervisionSink | None = None
        self._task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._cmds: asyncio.Queue[tuple[AsyncDaemonClient, dict[str, Any]]] | None = None
        self._closing = False
        self.connected_event = asyncio.Event()

    # ---------------------------------------------------------------- connect

    async def _open(self) -> AsyncDaemonClient | None:
        client = AsyncDaemonClient(self.sock_path, timeout=self.request_timeout)
        try:
            await asyncio.wait_for(client.connect(), self.connect_timeout)
            reply = await client.request(
                "hello", client="launcher", timeout=self.connect_timeout, version=_version()
            )
        except (DaemonUnavailable, TimeoutError, OSError):
            await client.close()
            return None
        if not reply.get("ok"):
            await client.close()
            return None
        return client

    async def connect(self) -> bool:
        """First connection before claude starts. Prints the ADR-0006 hints on failure."""
        client = await self._open()
        if client is None:
            try:
                installed = self.launchd.is_installed()
            except Exception:
                installed = False
            if not installed:
                self.state = "unsupervised"
                self.err(
                    "ccs: supervisor not installed — running unsupervised (ccs daemon install)"
                )
                return False
            try:
                self.launchd.start()
            except Exception as exc:
                log.info("daemon start failed: %s", exc)
            for delay in self.start_retries_s:
                await asyncio.sleep(delay)
                client = await self._open()
                if client is not None:
                    break
            if client is None:
                self.state = "retrying"
                self.err("ccs: supervisor not responding — running unsupervised")
                return False
        self.client = client
        self.state = "connected"
        self.connected_event.set()
        return True

    async def profile_status(self) -> dict[str, Any] | None:
        """The daemon `status` reply for this profile (None when unavailable)."""
        if self.client is None:
            return None
        try:
            reply = await self.client.request("status", profile_id=self.profile_id)
        except DaemonUnavailable:
            return None
        return reply if reply.get("ok") else None

    # ---------------------------------------------------------------- registration

    def start(
        self,
        reg: Registration,
        on_cmd: CmdHandler,
        on_supervision: SupervisionSink | None = None,
    ) -> None:
        """Register (now or once reachable) and keep the link alive in the background."""
        self._reg = reg
        self._on_cmd = on_cmd
        self._on_supervision = on_supervision
        if self.state == "unsupervised":
            return
        self._cmds = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._cmd_worker())
        self._task = asyncio.ensure_future(self._maintain())

    async def _register(self, client: AsyncDaemonClient, first: bool) -> bool:
        assert self._reg is not None
        reg = self._reg
        reply = await client.request(
            "register_wrapper",
            wrapper_id=reg.wrapper_id,
            profile_id=reg.profile_id,
            wrapper_pid=reg.wrapper_pid,
            claude_pid=reg.claude_pid,
            cwd=reg.cwd,
            started_overridden=reg.started_overridden and first,
        )
        if not reply.get("ok"):
            log.warning("register_wrapper refused: %s", reply.get("error"))
            return False
        self.registered = True
        if self._on_supervision is not None:
            with contextlib.suppress(Exception):
                self._on_supervision(reply)
        return True

    async def _maintain(self) -> None:
        first = True
        attempt = 0
        while not self._closing:
            client = self.client
            if client is None:
                client = await self._open()
                if client is None:
                    await asyncio.sleep(self.backoff_s[min(attempt, len(self.backoff_s) - 1)])
                    attempt += 1
                    continue
                self.client = client
                self.state = "connected"
                self.connected_event.set()
            attempt = 0
            try:
                if not await self._register(client, first):
                    self.state = "unsupervised"
                    return
                first = False
                await self._pump(client)
            except DaemonUnavailable:
                pass
            self.registered = False
            if self.client is client:
                self.client = None
            self.connected_event.clear()
            await client.close()
            if self._closing:
                return
            self.state = "retrying"
            await asyncio.sleep(self.backoff_s[0])
            attempt = 1

    async def _pump(self, client: AsyncDaemonClient) -> None:
        while True:
            msg = await client.next_push()
            if msg is None:
                return
            cmd = msg.get("cmd")
            if isinstance(cmd, dict) and self._cmds is not None:
                self._cmds.put_nowait((client, cmd))

    async def _cmd_worker(self) -> None:
        assert self._cmds is not None
        while True:
            client, cmd = await self._cmds.get()
            if self._on_cmd is None:
                continue
            result, detail = await self._on_cmd(cmd)
            with contextlib.suppress(DaemonUnavailable):
                await client.send({"ack": cmd.get("cmd_id"), "result": result, "detail": detail})

    # ---------------------------------------------------------------- events / close

    async def wrapper_event(self, kind: str, detail: dict[str, Any]) -> bool:
        """Report `input_submitted_while_paused` / `injected` / `inject_failed` (best effort).

        True only when the daemon accepted it; False when not registered, down or too slow.
        """
        client = self.client
        if client is None or not self.registered:
            return False
        try:
            reply = await client.request(
                "wrapper_event",
                wrapper_id=self.wrapper_id,
                kind=kind,
                detail=detail,
                timeout=2.0,
            )
        except DaemonUnavailable:
            return False
        return reply.get("ok") is True

    async def close(self, exit_code: int | None) -> None:
        """Unregister (1 s, best effort) and stop the background tasks."""
        self._closing = True
        client = self.client
        if client is not None and self.registered:
            with contextlib.suppress(DaemonUnavailable):
                await client.request(
                    "unregister_wrapper",
                    wrapper_id=self.wrapper_id,
                    exit_code=exit_code,
                    timeout=1.0,
                )
        for task in (self._task, self._worker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if client is not None:
            await client.close()
        self.client = None
        self.registered = False


def _version() -> str:
    from ccs import __version__

    return __version__
