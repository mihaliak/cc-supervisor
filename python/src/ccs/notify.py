"""Notification fallback (ADR-0015): osascript when no menu bar app is subscribed.

`EventBus.emit` has already computed `data.title`, `data.body` and `data.notify` (toggles,
dedupe, hour buckets). This dispatcher computes nothing: for `notify == true` events it
posts through `osascript` only when no connected client identified itself as `app`
(the app posts natively, P10).

`CCS_OSASCRIPT` overrides the osascript binary (tests point it at `true`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Coroutine, Iterable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)

OSASCRIPT_TIMEOUT_S = 5.0

Runner = Callable[[list[str]], Coroutine[Any, Any, int]]


class _Client(Protocol):
    client: str | None
    closed: bool


def applescript_string(text: str) -> str:
    """A double-quoted AppleScript string literal (backslash and quote escaped)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return f'"{escaped}"'


def osascript_argv(title: str, body: str, binary: str | None = None) -> list[str]:
    """`osascript -e 'display notification "<body>" with title "<title>"'`."""
    body_s, title_s = applescript_string(body), applescript_string(title)
    script = f"display notification {body_s} with title {title_s}"
    return [binary or os.environ.get("CCS_OSASCRIPT") or "osascript", "-e", script]


async def run_osascript(argv: list[str]) -> int:
    """Run one osascript (5 s timeout); returns its exit code (or -1)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("osascript notification failed to start: %s", exc)
        return -1
    try:
        _, err = await asyncio.wait_for(proc.communicate(), OSASCRIPT_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.warning("osascript notification timed out")
        return -1
    if proc.returncode:
        log.warning("osascript notification failed (%s): %s", proc.returncode, err.decode()[:200])
    return proc.returncode if proc.returncode is not None else -1


def app_connected(conns: Iterable[_Client]) -> bool:
    """Whether a menu bar app is connected (it posts notifications itself)."""
    return any(c.client == "app" and not c.closed for c in conns)


class Dispatcher:
    """EventBus subscriber: osascript fallback for `notify == true` events."""

    def __init__(
        self,
        has_app: Callable[[], bool],
        runner: Runner | None = None,
    ) -> None:
        self.has_app = has_app
        self.runner: Runner = runner or run_osascript
        self._tasks: set[asyncio.Task[Any]] = set()

    def __call__(self, record: dict[str, Any]) -> None:
        data = record.get("data")
        if not isinstance(data, dict) or data.get("notify") is not True:
            return
        if self.has_app():
            return
        title = data.get("title") if isinstance(data.get("title"), str) else ""
        body = data.get("body") if isinstance(data.get("body"), str) else ""
        if not title:
            return
        argv = osascript_argv(str(title), str(body))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop (synchronous caller): skip quietly
            log.debug("no event loop; notification %s not posted", record.get("type"))
            return
        task: asyncio.Task[int] = loop.create_task(self.runner(argv))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)


def install(daemon: Daemon) -> Dispatcher:
    """Daemon extension entry point: subscribe the fallback dispatcher to the event bus."""
    dispatcher = Dispatcher(lambda: app_connected(daemon.conns))
    daemon.events.subscribe(dispatcher)
    return dispatcher
