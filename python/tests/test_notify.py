"""osascript fallback dispatcher (ADR-0015, P06)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from ccs import notify


@dataclass
class FakeConn:
    client: str | None
    closed: bool = False
    topics: set[str] = field(default_factory=lambda: {"events", "snapshot"})


def record(notify_flag: bool = True, title: str = 'Work "x"', body: str = "a\\b") -> dict[str, Any]:
    return {"type": "limit.warn", "data": {"notify": notify_flag, "title": title, "body": body}}


def run_dispatch(records: list[dict[str, Any]], has_app: bool) -> list[list[str]]:
    seen: list[list[str]] = []

    async def runner(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    async def main() -> None:
        d = notify.Dispatcher(lambda: has_app, runner)
        for r in records:
            d(r)
        await d.drain()

    asyncio.run(main())
    return seen


def test_posts_escaped_text_without_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCS_OSASCRIPT", "/usr/bin/osascript")
    (argv,) = run_dispatch([record()], has_app=False)
    assert argv[0] == "/usr/bin/osascript" and argv[1] == "-e"
    assert argv[2] == 'display notification "a\\\\b" with title "Work \\"x\\""'


def test_not_posted_when_app_connected() -> None:
    assert run_dispatch([record()], has_app=True) == []


def test_not_posted_when_notify_false_or_no_title() -> None:
    assert run_dispatch([record(False), record(title="")], has_app=False) == []


def test_app_connected() -> None:
    assert notify.app_connected([FakeConn("launcher"), FakeConn("app")])
    assert not notify.app_connected([FakeConn("app", closed=True), FakeConn("cli")])


def test_app_without_events_subscription_does_not_count() -> None:
    """Only an app that listens for events can post them; otherwise the fallback does."""
    assert not notify.app_connected([FakeConn("app", topics=set())])
    assert not notify.app_connected([FakeConn("app", topics={"snapshot"})])
    assert notify.app_connected([FakeConn("app", topics={"events"})])


def test_applescript_string() -> None:
    assert notify.applescript_string('a "b"\nc\\') == '"a \\"b\\" c\\\\"'


def test_no_loop_is_quiet() -> None:
    d = notify.Dispatcher(lambda: False)
    d(record())  # no running event loop → skipped, never raises


def test_real_runner_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCS_OSASCRIPT", "true")
    assert asyncio.run(notify.run_osascript(notify.osascript_argv("t", "b"))) == 0
    assert asyncio.run(notify.run_osascript(["/nonexistent/osascript"])) == -1
