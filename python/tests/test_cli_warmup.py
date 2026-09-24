"""`ccs warmup` + the scheduler extension end to end under an in-process daemon (P08)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import ThreadedDaemon, scenario, short_state_dir, write_config

from ccs import paths
from ccs.cli import main
from ccs.clock import local_tz
from ccs.config import store
from ccs.daemon import extensions, scheduler
from ccs.events import read_tail
from ccs.snapshot import read_widget_snapshot
from ccs.warmup.cli import describe

FIXTURES = Path(__file__).parent / "fixtures" / "get_usage"
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def no_window_payload() -> dict[str, Any]:
    data = json.loads((FIXTURES / "ok_no_window.json").read_text())
    payload = data["response"]["response"]
    assert isinstance(payload, dict)
    return payload


def far_time() -> str:
    """A schedule time ~12 h away, so the startup look-back never fires it."""
    t = (datetime.now(UTC) + timedelta(hours=12)).astimezone(local_tz())
    return f"{t.hour:02d}:{t.minute:02d}"


def wait_for(pred: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


def event_types() -> list[str]:
    return [str(e.get("type")) for e in read_tail(limit=200)]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        scenario(
            tmp_path,
            monkeypatch,
            {
                "stream": {"get_usage": {"mode": "ok", "payload": no_window_payload()}},
                "print": {"stdout": "ok", "activate_window_s": 18000},
            },
        )
        write_config(
            tmp_path,
            extra_profile={
                "warmup": {"triggers": {"schedule": [{"time": far_time(), "weekdays": ALL_DAYS}]}}
            },
        )
        paths.ensure_state_layout()
        yield d


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_scheduler_is_a_registered_extension() -> None:
    assert "ccs.daemon.scheduler" in extensions.EXTENSIONS
    assert scheduler.install in extensions.resolve(extensions.EXTENSIONS)


def test_warmup_end_to_end(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedDaemon(extensions=[scheduler.install]):
        wait_for(lambda: paths.usage_file("work").exists())
        code, reply = run_cli(capsys, "warmup", "--profile", "work")
        assert code == 0, reply
        assert reply["ok"] and reply["trigger"] == "manual"
        assert reply["results"] == [{"profile_id": "work", "decision": "started", "reason": None}]
        wait_for(lambda: "warmup.succeeded" in event_types())
        types = event_types()
        assert types.index("warmup.started") < types.index("warmup.succeeded")
        done = next(e for e in read_tail(limit=200) if e["type"] == "warmup.succeeded")
        assert done["data"]["resets_at"] and done["data"]["notify"] is True
        assert done["data"]["title"]  # EventBus computed the notification fields
        state = json.loads(paths.warmup_file("work").read_text())
        assert state["last_attempt"]["result"] == "succeeded"
        assert state["next_scheduled_at"]

        def next_warmup() -> Any:
            doc = read_widget_snapshot()
            return doc["profiles"][0]["supervisor"]["next_warmup_at"] if doc else None

        wait_for(lambda: next_warmup() is not None)

        # the new window is active now: a second manual warm-up is skipped (rule 3)
        code, again = run_cli(capsys, "warmup", "--all")
        assert code == 0
        assert again["results"][0]["decision"] == "skipped"
        assert again["results"][0]["reason"] == "window_active"
        assert again["results"][0]["resets_at"]

        code, forced = run_cli(capsys, "warmup", "--profile", "work", "--force")
        assert forced["results"][0]["reason"] is None


def test_status_shows_next_warmup(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedDaemon(extensions=[scheduler.install]):
        wait_for(lambda: paths.usage_file("work").exists())
        code = main(["status", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["profiles"][0]["next_warmup_at"]


def test_daemon_down(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, reply = run_cli(capsys, "warmup", "--profile", "work")
    assert code == 1
    assert "ccs daemon start" in reply["error"]


def test_unknown_profile_and_missing_target(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, reply = run_cli(capsys, "warmup", "--profile", "nope")
    assert code == 2 and "unknown profile" in reply["error"]
    assert main(["warmup"]) == 2  # --profile or --all is required
    capsys.readouterr()


def test_describe_lines(env: Path) -> None:
    cfg = store.load()[0]
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    assert describe(cfg, {"profile_id": "work", "decision": "started"}) == (
        "💼 work: started (window will be confirmed; see ccs events --follow)"
    )
    line = describe(
        cfg,
        {"profile_id": "work", "decision": "skipped", "reason": "window_active", "resets_at": soon},
    )
    assert line.startswith("💼 work: skipped (window_active, resets ")
    assert describe(cfg, {"profile_id": "x", "decision": "skipped", "reason": "cooldown"}) == (
        "x: skipped (cooldown)"
    )
