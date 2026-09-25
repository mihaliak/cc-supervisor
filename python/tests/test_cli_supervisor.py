"""`ccs pause`, `ccs resume`, `ccs sessions` against an in-process daemon (P06)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import ThreadedDaemon, scenario, short_state_dir, write_config
from supervisor_helpers import set_usage, usage_payload

from ccs.cli import main
from ccs.daemon.client import DaemonClient
from ccs.supervisor import engine as engine_mod
from ccs.supervisor.cli import collect_sessions, match_sessions, resolve_session

WID = "abcdef12-3456-7890-abcd-ef1234567890"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_state_dir(monkeypatch):
        scenario(tmp_path, monkeypatch, {})
        scen = tmp_path / "fake-scenario.json"
        reset = datetime.now(UTC) + timedelta(hours=2)
        set_usage(scen, usage_payload((20, reset)))
        write_config(tmp_path)
        yield scen


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


def register_and_leave(wid: str = WID) -> None:
    """A launcher that registers and disconnects (its record stays; commands can't reach it)."""
    with DaemonClient(timeout=5) as c:
        assert c.hello("launcher")["ok"]
        reply = c.request(
            "register_wrapper",
            wrapper_id=wid,
            profile_id="work",
            wrapper_pid=os.getpid(),
            claude_pid=os.getpid(),
            cwd=str(Path.home() / "Code" / "proj"),
        )
        assert reply["ok"]


def test_pause_resume_need_the_daemon(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "pause", "--profile", "work")
    assert code == 1 and "supervisor not running" in out["error"]
    assert main(["resume", "--profile", "nope"]) == 2


def test_sessions_without_daemon_reads_files(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "sessions")
    assert code == 0 and out["daemon_responsive"] is False and out["sessions"] == []
    assert main(["sessions"]) == 0
    assert "no supervised ccs sessions" in capsys.readouterr().out


def test_pause_resume_sessions_cycle(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedDaemon(extensions=[engine_mod.install]):
        register_and_leave()
        code, out = run_json(capsys, "pause", "--session", WID[:8])
        assert code == 0 and out["ok"] and out["wrapper_id"] == WID
        assert out["hold"]["scope"] == WID
        code, out = run_json(capsys, "sessions", "--profile", "work")
        assert code == 0 and out["daemon_responsive"] is True
        (row,) = out["sessions"]
        assert row["wrapper_id"] == WID and row["state"] == "paused"
        assert row["holds"] == ["manual"] and row["resume_at"] is None
        assert main(["sessions"]) == 0
        human = capsys.readouterr().out
        assert "abcdef12" in human and "⏸ paused (manual)" in human and "~/Code/proj" in human
        code, out = run_json(capsys, "resume", "--session", WID)
        assert code == 0 and out["sessions_resumed"] == 1
        assert [h["id"] for h in out["cleared"]] == ["manual"]
        code, out = run_json(capsys, "pause", "--profile", "work")
        assert code == 0 and out["created"] is True
        assert main(["pause", "--profile", "work"]) == 0
        assert "already paused manually" in capsys.readouterr().out
        assert main(["resume", "--profile", "work"]) == 0
        assert "resumed" in capsys.readouterr().out
        code, out = run_json(capsys, "pause", "--session", "zzz")
        assert code == 2 and "no supervised session" in out["error"]


def test_resolve_session_and_collect() -> None:
    status = {
        "profiles": [
            {
                "id": "work",
                "sessions": [
                    {"wrapper_id": "aaaa1111", "supervision": {"state": "paused"}},
                    {"wrapper_id": "aaaa2222"},
                ],
                "other_sessions": {"interactive": 1, "background": 2},
            },
            {"id": "personal", "sessions": [], "other_sessions": None},
        ]
    }
    assert resolve_session(status, "aaaa1111") == "aaaa1111"
    assert resolve_session(status, "aaaa2") == "aaaa2222"
    assert resolve_session(status, "aaaa") is None  # ambiguous
    assert resolve_session(None, "x") is None
    data = collect_sessions(status, "work")
    assert [r["state"] for r in data["sessions"]] == ["paused", "running"]
    assert data["other_sessions"] == {"work": {"interactive": 1, "background": 2}}


def test_ambiguous_session_prefix_lists_candidates(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "abcdef99-0000-0000-0000-000000000000"
    with ThreadedDaemon(extensions=[engine_mod.install]):
        register_and_leave()
        register_and_leave(other)
        code, out = run_json(capsys, "pause", "--session", "abcdef")
        assert code == 2 and "ambiguous" in out["error"]
        assert WID[:8] in out["error"] and other[:8] in out["error"]
        assert "no supervised session" not in out["error"]
        assert main(["resume", "--session", "abcdef"]) == 2
        err = capsys.readouterr().err
        assert "ambiguous" in err and "abcdef12" in err and "abcdef99" in err
        # a longer prefix still resolves
        code, out = run_json(capsys, "pause", "--session", "abcdef1")
        assert code == 0 and out["wrapper_id"] == WID


def test_match_sessions() -> None:
    status = {
        "profiles": [{"id": "work", "sessions": [{"wrapper_id": "ab1"}, {"wrapper_id": "ab2"}]}]
    }
    assert match_sessions(status, "ab") == ["ab1", "ab2"]
    assert match_sessions(status, "ab1") == ["ab1"]
    assert match_sessions(status, "zz") == []
    assert match_sessions(None, "ab") == []


def test_ambiguous_message_ids_are_distinct() -> None:
    from ccs.supervisor.cli import ambiguous_message

    msg = ambiguous_message("abc", ["abcdefgh-1", "abcdefgh-2"])
    assert "abcdefgh-1, abcdefgh-2" in msg
