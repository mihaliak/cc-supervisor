"""Poller: cadence table, coalesced forced polls, status-transition events, failure writes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from daemon_helpers import (
    calls,
    run_with_daemon,
    scenario,
    short_state_dir,
    wait_until,
    write_config,
)

from ccs import paths
from ccs.config.models import Polling
from ccs.daemon.poller import interval_for, max_percent
from ccs.daemon.server import Daemon
from ccs.snapshot import read_widget_snapshot
from ccs.usage.model import ScopedWindow, UsageSnapshot, Window

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
POLLING = Polling(60, 20, 70, 120)


def snap(session: int, weekly: int = 0, scoped: int | None = None) -> UsageSnapshot:
    return UsageSnapshot(
        profile_id="work",
        status="ok",
        session=Window(session, None, NOW),
        weekly=Window(weekly, None, NOW),
        model_scoped=(ScopedWindow("Fable", scoped, None, NOW),) if scoped is not None else (),
    )


@pytest.mark.parametrize(
    ("snapshot", "activities", "expected"),
    [
        (snap(90), [], 120),  # no wrappers → idle
        (snap(10), ["busy"], 60),
        (snap(69), ["busy"], 60),
        (snap(70), ["busy"], 20),
        (snap(70), ["idle"], 60),  # all idle → normal
        (snap(70), ["idle", "shell"], 20),
        (snap(10, weekly=75), ["busy"], 20),
        (snap(10, scoped=71), ["waiting"], 20),
        (snap(70), ["unknown"], 20),
        (None, ["busy"], 60),
    ],
)
def test_interval_for(snapshot: UsageSnapshot | None, activities: list[str], expected: int) -> None:
    assert interval_for(POLLING, snapshot, activities) == expected


def test_max_percent() -> None:
    assert max_percent(snap(10, 20, 30)) == 30
    assert max_percent(None) is None


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        yield d


def test_startup_polls_once_and_writes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    log = scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: paths.usage_file("work").exists())
        usage = json.loads(paths.usage_file("work").read_text())
        assert usage["status"] == "ok" and usage["polled_at"]

        def widget_rows() -> list[dict[str, object]]:
            doc = read_widget_snapshot() or {}
            profiles = doc.get("profiles") or [{}]
            rows = profiles[0].get("rows") or []
            return list(rows)

        await wait_until(lambda: bool(widget_rows()))
        assert widget_rows()[0]["kind"] == "session"

    run_with_daemon(body)
    assert len(calls(log, "stream")) == 1


def test_forced_requests_during_probe_coalesce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    log = scenario(
        tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "slow", "delay_s": 0.8}}}
    )
    write_config(tmp_path)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: len(calls(log, "stream")) == 1)  # startup probe in flight
        futures = [daemon.request_poll("work") for _ in range(3)]
        results = await asyncio.wait_for(asyncio.gather(*futures), 10)
        assert all(r is not None for r in results)
        assert results[0] is results[1] is results[2]
        await asyncio.sleep(0.3)

    run_with_daemon(body)
    assert len(calls(log, "stream")) == 2  # startup + exactly one follow-up


def test_request_poll_at_future_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    log = scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: len(calls(log, "stream")) == 1)
        await asyncio.sleep(0.2)
        at = daemon.clock.now().replace(microsecond=0)
        from datetime import timedelta

        fut = daemon.request_poll("work", at + timedelta(seconds=1.5))
        await asyncio.sleep(0.5)
        assert not fut.done()
        await asyncio.wait_for(fut, 5)

    run_with_daemon(body)
    assert len(calls(log, "stream")) == 2


def test_unknown_profile_request_resolves_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path)

    async def body(daemon: Daemon) -> None:
        assert await daemon.request_poll("nope") is None

    run_with_daemon(body)


def test_needs_sign_in_emits_auth_required_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    scenario(
        tmp_path,
        monkeypatch,
        {
            "stream": {"get_usage": {"mode": "logged_out"}},
            "auth_status": {"json": {"loggedIn": False}, "exit": 1},
        },
    )
    write_config(tmp_path)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: daemon.snapshots.get("work") is not None)
        await daemon.request_poll("work")
        await daemon.request_poll("work")

    run_with_daemon(body)
    events = [json.loads(x) for x in paths.events_file().read_text().splitlines()]
    auth = [e for e in events if e["type"] == "auth.required"]
    assert len(auth) == 1
    assert auth[0]["key"].startswith("auth:work:")
    assert auth[0]["data"]["notify"] is True
    usage = json.loads(paths.usage_file("work").read_text())
    assert usage["status"] == "needs_sign_in"


def test_failed_poll_keeps_windows_and_bumps_polled_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    spec = {"stream": {"get_usage": {"mode": "ok"}}}
    scen = scenario(tmp_path, monkeypatch, spec)
    write_config(tmp_path)
    scen_file = tmp_path / "fake-scenario.json"

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: paths.usage_file("work").exists())
        first = json.loads(paths.usage_file("work").read_text())
        scen_file.write_text(json.dumps({"stream": {"get_usage": {"mode": "malformed"}}}))
        await asyncio.sleep(1.1)  # polled_at has 1 s resolution
        await daemon.request_poll("work")
        second = json.loads(paths.usage_file("work").read_text())
        assert second["status"] == "source_error"
        assert second["windows"]["session"] == first["windows"]["session"]
        assert second["polled_at"] > first["polled_at"]
        assert second["fetched_at"] == first["fetched_at"]

    run_with_daemon(body)
    assert scen.exists()
    events = [json.loads(x) for x in paths.events_file().read_text().splitlines()]
    assert [e["type"] for e in events if e["type"] == "usage.source_error"] == [
        "usage.source_error"
    ]


def test_loop_survives_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug outside `poll_once` is logged, fails the waiting polls, and the loop keeps going."""
    from ccs.daemon import poller as poller_mod

    log = scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path)
    monkeypatch.setattr(poller_mod, "ERROR_BACKOFF_S", (0.05,), raising=False)
    real = poller_mod.interval_for
    failures = {"left": 0}

    def flaky(*args: object) -> int:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("boom")
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(poller_mod, "interval_for", flaky)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: len(calls(log, "stream")) == 1)
        failures["left"] = 3  # a few consecutive failures: backoff, not a hot loop
        assert await asyncio.wait_for(daemon.request_poll("work"), 5) is None  # not hanging
        await wait_until(lambda: failures["left"] == 0)
        snap = await asyncio.wait_for(daemon.request_poll("work"), 5)  # loop still alive
        assert snap is not None

    with caplog.at_level("ERROR", logger="ccs.daemon.poller"):
        run_with_daemon(body)
    assert "poll loop of work failed" in caplog.text
    assert len(calls(log, "stream")) == 2


class _Escaped(BaseException):
    """Not an `Exception`: escapes the loop's own guard and ends the task."""


def test_dead_loop_fails_waiters_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A loop task that dies anyway is logged, resolves its waiters, and is started again."""
    from ccs.daemon import poller as poller_mod

    log = scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path)
    monkeypatch.setattr(poller_mod, "RESTART_DELAY_S", 0.05, raising=False)
    real = poller_mod.interval_for
    escape = {"now": False}

    def fatal(*args: object) -> int:
        if escape["now"]:
            escape["now"] = False
            raise _Escaped()
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(poller_mod, "interval_for", fatal)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: len(calls(log, "stream")) == 1)
        st = daemon.poller._states["work"]
        first_task = st.task
        later = daemon.request_poll("work", daemon.clock.now().replace(year=2099))
        escape["now"] = True
        await wait_until(lambda: first_task is not None and first_task.done())
        assert await asyncio.wait_for(later, 5) is None  # not left hanging
        await wait_until(lambda: st.task is not first_task and st.task is not None)
        assert await asyncio.wait_for(daemon.request_poll("work"), 5) is not None

    with caplog.at_level("ERROR", logger="ccs.daemon.poller"):
        run_with_daemon(body)
    assert "poll loop of work died" in caplog.text
