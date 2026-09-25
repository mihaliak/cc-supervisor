"""`ccs.statusline.runtime`: IO shell, live reports, offline detection, fallback."""

from __future__ import annotations

import io
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from schema_check import validate
from sl_helpers import NOW, SESSION_RESET, load_fixture, strip_ansi, usage_doc

from ccs import paths
from ccs.fsio import atomic_write_json
from ccs.statusline import runtime

CONSTANTS: dict[str, Any] = {
    "generator_version": 1,
    "profile": {"id": "work", "name": "Work", "emoji": "💼"},
    "limits": {},
    "colors": {"yellow_from": 50, "red_from": 80},
    "state_dir": "/nonexistent/baked",
}


def stdin_json(session: float | None = 45.0, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "session_id": "sess-1",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "effort": {"level": "xhigh"},
        "workspace": {"current_dir": "/Users/you/Code/project"},
    }
    if session is not None:
        data["rate_limits"] = {
            "five_hour": {"used_percentage": session, "resets_at": int(SESSION_RESET.timestamp())},
            "seven_day": {"used_percentage": 50.0, "resets_at": int(SESSION_RESET.timestamp())},
        }
    data.update(extra)
    return data


def run(
    state: Path,
    data: Any,
    *,
    wrapper: str | None = "w1",
    now: Any = NOW,
    raw: str | None = None,
) -> str:
    env = {"CCS_STATE_DIR": str(state)}
    if wrapper:
        env["CCS_WRAPPER_ID"] = wrapper
    out = io.StringIO()
    text = raw if raw is not None else json.dumps(data)
    rc = runtime.main(CONSTANTS, stdin=io.StringIO(text), stdout=out, env=env, now=now)
    assert rc == 0
    value = out.getvalue()
    assert value.endswith("\n")
    return strip_ansi(value.rstrip("\n"))


def fresh_usage(state: Path, **kw: Any) -> Path:
    path = state / "usage" / "work.json"
    atomic_write_json(path, usage_doc(**kw))
    return path


def test_renders_and_writes_live_report(tmp_path: Path) -> None:
    fresh_usage(tmp_path)
    line = run(tmp_path, stdin_json(45.0))
    assert line.startswith("💼 Work ~ project ~ Opus 5.5 / xhigh ~ 45% ▓▓▓▓▓░░░░░ ")
    assert "supervisor offline" not in line
    report = json.loads((tmp_path / "live" / "w1.json").read_text())
    assert validate(report, "live-report.schema.json") == []
    assert report["profile_id"] == "work"
    assert report["wrapper_id"] == "w1"
    assert report["session_id"] == "sess-1"
    assert report["model_id"] == "claude-opus-5-5"
    assert report["effort"] == "xhigh"
    assert report["cwd"] == "/Users/you/Code/project"
    assert report["observed_at"] == "2026-09-24T15:47:00Z"
    assert report["rate_limits"]["five_hour"] == {
        "percent": 45.0,
        "resets_at": "2026-09-24T18:00:00Z",
    }


def test_live_report_is_parsed_by_p03(tmp_path: Path) -> None:
    from ccs.usage.merge import parse_live_report

    run(tmp_path, stdin_json(61.6))
    parsed = parse_live_report(json.loads((tmp_path / "live" / "w1.json").read_text()))
    assert parsed is not None and parsed.five_hour is not None
    assert parsed.five_hour.percent == 62
    assert parsed.five_hour.resets_at == SESSION_RESET


def test_live_report_rewrite_rules(tmp_path: Path) -> None:
    path = tmp_path / "live" / "w1.json"
    run(tmp_path, stdin_json(45.0))
    first = json.loads(path.read_text())
    run(tmp_path, stdin_json(45.0), now=NOW + timedelta(seconds=59))
    assert json.loads(path.read_text()) == first  # unchanged within 60 s
    run(tmp_path, stdin_json(45.0), now=NOW + timedelta(seconds=61))
    assert json.loads(path.read_text())["observed_at"] == "2026-09-24T15:48:01Z"
    run(tmp_path, stdin_json(46.0), now=NOW + timedelta(seconds=62))
    assert json.loads(path.read_text())["rate_limits"]["five_hour"]["percent"] == 46.0
    leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_live_report_without_wrapper_uses_session_id(tmp_path: Path) -> None:
    run(tmp_path, stdin_json(45.0), wrapper=None)
    assert (tmp_path / "live" / "session-sess-1.json").is_file()


def test_no_rate_limits_writes_nothing(tmp_path: Path) -> None:
    run(tmp_path, load_fixture("no_rate_limits_yet.json"))
    assert not (tmp_path / "live").exists()


def test_percent_clamped_in_report(tmp_path: Path) -> None:
    run(tmp_path, stdin_json(130.0))
    report = json.loads((tmp_path / "live" / "w1.json").read_text())
    assert report["rate_limits"]["five_hour"]["percent"] == 100.0


@pytest.mark.parametrize("raw", ["", "not json", "[1,2]", "null", '{"rate_limits": 5}'])
def test_garbage_stdin_renders_no_data(tmp_path: Path, raw: str) -> None:
    line = run(tmp_path, None, raw=raw)
    assert line == "💼 Work ~ ?% ░░░░░░░░░░ ~ ⚠ supervisor offline"


def test_exception_prints_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "render", boom)
    assert run(tmp_path, stdin_json()) == "💼 Work ~ ?%"


def test_offline_detection(tmp_path: Path) -> None:
    assert run(tmp_path, stdin_json()).endswith("~ ⚠ supervisor offline")  # missing
    path = fresh_usage(tmp_path)
    os.utime(path, (NOW.timestamp() - 10, NOW.timestamp() - 10))
    assert "supervisor offline" not in run(tmp_path, stdin_json())
    os.utime(path, (NOW.timestamp() - 301, NOW.timestamp() - 301))
    assert run(tmp_path, stdin_json()).endswith("~ ⚠ supervisor offline")


def test_wrapper_record_drives_supervision(tmp_path: Path) -> None:
    usage = fresh_usage(tmp_path)
    os.utime(usage, (NOW.timestamp(), NOW.timestamp()))
    assert "⏸" not in run(tmp_path, stdin_json(90.0))  # record missing: registration lag
    atomic_write_json(
        tmp_path / "sessions" / "w1.json",
        {
            "schema": 1,
            "wrapper_id": "w1",
            "supervision": {"state": "paused", "holds": ["session"], "resume_at": None},
        },
    )
    line = run(tmp_path, stdin_json(90.0))
    assert line.endswith("⏸ 90% ▓▓▓▓▓▓▓▓▓░ paused (manual)")
    assert "⏸" not in run(tmp_path, stdin_json(90.0), wrapper=None)


def test_state_dir_env_overrides_baked(tmp_path: Path) -> None:
    assert runtime.state_dir_for(CONSTANTS, {"CCS_STATE_DIR": str(tmp_path)}) == str(tmp_path)
    assert runtime.state_dir_for(CONSTANTS, {"CCS_STATE_DIR": "relative"}) == "/nonexistent/baked"
    assert runtime.state_dir_for(CONSTANTS, {}) == "/nonexistent/baked"


def test_unwritable_state_dir_still_prints(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a dir")
    line = run(blocked, stdin_json(45.0))
    assert "45% ▓▓▓▓▓░░░░░" in line


def test_usage_file_directory_is_tolerated(tmp_path: Path) -> None:
    (tmp_path / "usage" / "work.json").mkdir(parents=True)
    assert "45%" in run(tmp_path, stdin_json(45.0))


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("w1", True),
        ("A-z_0-9", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("", False),
        ("../x", False),
        ("a/b", False),
        ("..", False),
        ("x\n", False),
        ("ä", False),
        (None, False),
        (7, False),
    ],
)
def test_safe_id(value: Any, ok: bool) -> None:
    assert runtime.safe_id(value) == (value if ok else None)
    assert paths.is_valid_id(value) is ok  # the same rule as the daemon's


def test_id_pattern_matches_paths() -> None:
    assert runtime.ID_RE.pattern == paths.ID_RE.pattern


@pytest.mark.parametrize("wrapper", ["../../escaped", "a/b", "x\n"])
def test_unsafe_wrapper_id_builds_no_path(tmp_path: Path, wrapper: str) -> None:
    state = tmp_path / "state"
    (state / "sessions").mkdir(parents=True)
    # what `sessions/../../escaped.json` would read: a paused record outside the state dir
    atomic_write_json(
        tmp_path / "escaped.json",
        {"wrapper_id": wrapper, "supervision": {"state": "paused", "holds": ["session"]}},
    )
    line = run(state, stdin_json(45.0), wrapper=wrapper)
    assert "45% ▓▓▓▓▓░░░░░" in line and "⏸" not in line
    assert not (state / "live").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["escaped.json", "state"]


@pytest.mark.parametrize("session_id", ["../../escaped", "a/b", "..", "x" * 129])
def test_unsafe_session_id_builds_no_path(tmp_path: Path, session_id: str) -> None:
    state = tmp_path / "state"
    line = run(state, stdin_json(45.0, session_id=session_id), wrapper=None)
    assert "45% ▓▓▓▓▓░░░░░" in line
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere
