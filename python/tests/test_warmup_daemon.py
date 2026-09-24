"""Every warm-up trigger end to end under a real in-process daemon + fake claude (P08)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import run_with_daemon, scenario, short_state_dir, wait_until, write_config

from ccs import paths
from ccs.clock import local_tz
from ccs.daemon import scheduler
from ccs.daemon.server import Daemon
from ccs.events import read_tail

FIXTURES = Path(__file__).parent / "fixtures" / "get_usage"
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_DAY = {"start": "00:00", "end": "00:00"}


def no_window_payload() -> dict[str, Any]:
    payload = json.loads((FIXTURES / "ok_no_window.json").read_text())["response"]["response"]
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def scen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch):
        scenario(
            tmp_path,
            monkeypatch,
            {
                "stream": {"get_usage": {"mode": "ok", "payload": no_window_payload()}},
                "print": {"stdout": "ok", "activate_window_s": 18000},
            },
        )
        yield tmp_path / "fake-scenario.json.state.json"  # the fake's window state file


def configure(tmp_path: Path, **warm: Any) -> None:
    write_config(
        tmp_path,
        extra_profile={"warmup": {"active_hours": ALL_DAY, "cooldown_minutes": 0, **warm}},
    )


def fast_install(daemon: Daemon) -> scheduler.Scheduler:
    return scheduler.install(daemon, tick_s=0.1)


def succeeded(trigger: str) -> bool:
    return any(
        e.get("type") == "warmup.succeeded" and e.get("data", {}).get("trigger") == trigger
        for e in read_tail(limit=500)
    )


def test_app_start_and_unlock_wake(tmp_path: Path, scen: Path) -> None:
    configure(tmp_path)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        op = daemon.hooks.ops["warmup"]
        reply = await op(None, {"all": True, "trigger": "app_start"})  # type: ignore[arg-type]
        assert reply["results"][0]["decision"] == "started"
        await wait_until(lambda: succeeded("app_start"), timeout=10)
        # the window is active now → unlock is skipped …
        reply = await op(None, {"all": True, "trigger": "unlock_wake"})  # type: ignore[arg-type]
        assert reply["results"][0]["reason"] == "window_active"
        # … until it resets (the fake forgets it) and a fresh poll sees no window
        scen.unlink()
        await daemon.request_poll("work")
        reply = await op(None, {"all": True, "trigger": "unlock_wake"})  # type: ignore[arg-type]
        assert reply["results"][0]["decision"] == "started"
        await wait_until(lambda: succeeded("unlock_wake"), timeout=10)

    run_with_daemon(body, extensions=[fast_install])


def test_schedule_runs_under_daemon(tmp_path: Path, scen: Path) -> None:
    just_now = (datetime.now(UTC) - timedelta(minutes=2)).astimezone(local_tz())
    hhmm = f"{just_now.hour:02d}:{just_now.minute:02d}"
    configure(tmp_path, triggers={"schedule": [{"time": hhmm, "weekdays": ALL_DAYS}]})

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: succeeded("schedule"), timeout=10)
        state = json.loads(paths.warmup_file("work").read_text())
        assert state["consumed"] and state["last_attempt"]["trigger"] == "schedule"
        assert state["next_scheduled_at"]  # tomorrow's occurrence

    run_with_daemon(body, extensions=[fast_install])


def test_auto_chain_runs_under_daemon(
    tmp_path: Path, scen: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler, "CHAIN_DELAY", timedelta(seconds=0.2))
    monkeypatch.setattr(scheduler, "CHAIN_RETRY", timedelta(seconds=0.2))
    configure(tmp_path)
    # an active window that resets in 1.5 s
    scen.write_text(json.dumps({"session": {"resets_at_epoch": time.time() + 1.5, "percent": 40}}))

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        await wait_until(
            lambda: time.time() > json.loads(scen.read_text())["session"]["resets_at_epoch"] + 0.5,
            timeout=5,
        )
        scen.unlink()  # the window has reset: the next confirmation poll sees none
        await wait_until(lambda: succeeded("auto_chain"), timeout=15)

    run_with_daemon(body, extensions=[fast_install])
