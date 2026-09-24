"""Helpers for in-process daemon tests (short socket paths, fake claude, a thread runner)."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest

from ccs import paths
from ccs.config import store
from ccs.daemon.server import Daemon

T = TypeVar("T")

FAKE_CLAUDE = Path(__file__).parent / "fake_claude" / "claude"


@contextmanager
def short_state_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`CCS_STATE_DIR` under /tmp so `daemon.sock` stays below the 104-byte limit."""
    d = Path(tempfile.mkdtemp(prefix="ccs-t-", dir="/tmp"))
    monkeypatch.setenv("CCS_STATE_DIR", str(d))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def write_config(
    base: Path,
    *,
    profiles: tuple[str, ...] = ("work",),
    claude: Path = FAKE_CLAUDE,
    notifications: dict[str, bool] | None = None,
    extra_profile: dict[str, Any] | None = None,
) -> Path:
    """Create `config.json` (under the test's XDG config home) with temp config dirs."""
    entries = []
    for pid in profiles:
        cdir = base / f"profile-{pid}"
        cdir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "id": pid,
            "flag": pid,
            "name": pid.title(),
            "emoji": "💼",
            "config_dir": str(cdir),
        }
        entry.update(extra_profile or {})
        entries.append(entry)
    raw: dict[str, Any] = {
        "version": 1,
        "revision": 0,
        "default_profile": profiles[0] if profiles else None,
        "claude_path": str(claude),
        "profiles": entries,
    }
    if notifications is not None:
        raw["notifications"] = notifications
    store.create(raw)
    return paths.config_file()


def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: dict[str, Any]) -> Path:
    """Write a fake-claude scenario; return the call log path."""
    scen = tmp_path / "fake-scenario.json"
    log = tmp_path / "fake-calls.jsonl"
    scen.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(scen))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return log


def calls(log: Path, mode: str | None = None) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    out = [json.loads(line) for line in log.read_text("utf-8").splitlines() if line.strip()]
    return [c for c in out if mode is None or c.get("mode") == mode]


def make_daemon(**kw: Any) -> Daemon:
    """A daemon with fast loops, no sampler/reaper and no extensions (tests enable them)."""
    opts: dict[str, Any] = {
        "extensions": [],
        "enable_sampler": False,
        "enable_reaper": False,
        "tick_s": 0.05,
        "live_scan_s": 0.05,
        "config_scan_s": 0.05,
    }
    opts.update(kw)
    return Daemon(**opts)


async def wait_until(pred: Callable[[], bool], timeout: float = 5.0, step: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


def run_with_daemon[R](body: Callable[[Daemon], Awaitable[R]], **kw: Any) -> R:
    """Start a daemon in this event loop, run `body(daemon)`, always stop it."""

    async def main() -> R:
        daemon = make_daemon(**kw)
        await daemon.start()
        try:
            return await body(daemon)
        finally:
            await daemon.stop()

    return asyncio.run(main())


class ThreadedDaemon:
    """A daemon running `run()` in a background thread (for synchronous CLI tests)."""

    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.daemon: Daemon | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._main, daemon=True)

    def _main(self) -> None:
        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            self.daemon = make_daemon(**self.kw)
            await self.daemon.start()
            self._ready.set()
            try:
                await self.daemon._stop.wait()
            finally:
                await self.daemon.stop()

        asyncio.run(main())

    def __enter__(self) -> ThreadedDaemon:
        self._thread.start()
        if not self._ready.wait(10):
            raise AssertionError("daemon did not start")
        return self

    def call(self, fn: Callable[[], T]) -> T:
        """Run `fn` on the daemon loop and return its result."""
        assert self.loop is not None
        result: list[T] = []
        done = threading.Event()

        def run() -> None:
            result.append(fn())
            done.set()

        self.loop.call_soon_threadsafe(run)
        done.wait(5)
        return result[0]

    def __exit__(self, *exc: object) -> None:
        if self.loop is not None and self.daemon is not None:
            self.loop.call_soon_threadsafe(self.daemon.request_stop)
        self._thread.join(10)
