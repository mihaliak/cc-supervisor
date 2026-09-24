"""`claude_cli` probe + `source_claude` classification against the fake claude."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClaude
from usage_helpers import T0, make_profile, payload

from ccs.claude_cli import (
    ClaudeCliError,
    ClaudeNotFound,
    ClaudeTimeout,
    ProbeResult,
    agents_json,
    auth_status,
    probe_usage,
    profile_env,
    resolve_claude,
    run,
)
from ccs.clock import FakeClock
from ccs.config.models import Config
from ccs.usage.model import (
    STATUS_NEEDS_SIGN_IN,
    STATUS_NO_SUBSCRIPTION,
    STATUS_OK,
    STATUS_SOURCE_ERROR,
)
from ccs.usage.normalize import normalize
from ccs.usage.source_claude import classify, fetch_snapshot, needs_auth_check

MakeFake = Callable[[dict[str, Any]], FakeClaude]
LOGGED_OUT_AUTH = {"json": {"loggedIn": False, "authMethod": "none"}, "exit": 1}
LOGGED_IN_AUTH = {"json": {"loggedIn": True, "authMethod": "claude.ai"}, "exit": 0}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "claude-profile"
    d.mkdir()
    return d


def probe(fc: FakeClaude, config_dir: Path, **kw: Any) -> ProbeResult:
    return asyncio.run(probe_usage(str(fc.path), make_profile(config_dir), **kw))


def alive(pid: int | None) -> bool:
    assert pid is not None
    return subprocess.run(["/bin/ps", "-p", str(pid)], capture_output=True).returncode == 0


def test_ok_probe_passes_flags_and_env(
    fake_claude: MakeFake, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    monkeypatch.setenv("CCS_WRAPPER_ID", "w")
    fc = fake_claude({"stream": {"get_usage": {"mode": "ok"}}})
    result = probe(fc, config_dir)
    assert result.error is None and result.error_kind is None
    assert result.raw == payload("ok_max.json")
    assert result.rc == 0 and result.forced_kill is False
    assert not alive(result.pid)
    (call,) = fc.calls()
    argv = call["argv"]
    assert argv[:6] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    env = call["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
    for leaked in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CCS_WRAPPER_ID"):
        assert leaked not in env


def test_graceful_exit_leaves_no_registry_files(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"stream": {"registry": True, "get_usage": {"mode": "ok"}}})
    result = probe(fc, config_dir)
    assert result.raw is not None and result.forced_kill is False
    assert list((config_dir / "sessions").glob("*")) == []


def test_slow_beyond_timeout_is_source_error_and_reaped(
    fake_claude: MakeFake, config_dir: Path
) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "slow", "delay_s": 30}}})
    result = probe(fc, config_dir, timeout=0.5, graceful_s=0.3)
    assert result.raw is None and result.error_kind == "source_error"
    assert result.error is not None and "no get_usage response within 0.5s" in result.error
    assert result.forced_kill is True
    assert not alive(result.pid)


def test_slow_within_timeout_is_ok(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "slow", "delay_s": 0.3}}})
    assert probe(fc, config_dir, timeout=5).raw is not None


def test_forced_kill_removes_only_that_pids_registry_files(
    fake_claude: MakeFake, config_dir: Path
) -> None:
    sessions = config_dir / "sessions"
    sessions.mkdir()
    unrelated = [sessions / "1.json", sessions / "1.abc.key", sessions / "other.txt"]
    for f in unrelated:
        f.write_text("x")
    fc = fake_claude(
        {"stream": {"registry": True, "ignore_sigterm": True, "get_usage": {"mode": "no_response"}}}
    )
    result = probe(fc, config_dir, timeout=0.5, graceful_s=0.2)
    assert result.forced_kill is True and result.raw is None
    assert not alive(result.pid)
    assert sorted(p.name for p in sessions.iterdir()) == sorted(f.name for f in unrelated)


def test_no_response_then_eof_exit_is_graceful(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude(
        {"stream": {"registry": True, "get_usage": {"mode": "no_response", "exit_on_eof": True}}}
    )
    result = probe(fc, config_dir, timeout=0.5)
    assert result.error_kind == "source_error" and result.forced_kill is False
    assert list((config_dir / "sessions").glob("*")) == []


def test_exit_early_reports_rc_and_stderr(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "exit_early", "stderr": "fatal: nope"}}})
    result = probe(fc, config_dir)
    assert result.raw is None
    assert result.error is not None
    assert "rc=1" in result.error and "fatal: nope" in result.error


def test_error_subtype(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"stream": {"get_usage": {"error": "Unknown subtype"}}})
    result = probe(fc, config_dir)
    assert result.error is not None and "Unknown subtype" in result.error


def test_missing_executable(config_dir: Path, tmp_path: Path) -> None:
    result = asyncio.run(probe_usage(str(tmp_path / "nope"), make_profile(config_dir)))
    assert result.error_kind == "source_error"


# ------------------------------------------------------------- classification


def fetch(fc: FakeClaude, config_dir: Path, **kw: Any) -> Any:
    clock = FakeClock(T0)
    return asyncio.run(
        fetch_snapshot(None, make_profile(config_dir), clock=clock, claude=str(fc.path), **kw)
    )


def test_fetch_ok(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "ok"}}})
    snap = fetch(fc, config_dir)
    assert snap.status == STATUS_OK and snap.fetched_at == T0
    assert snap.session is not None and snap.session.percent == 15
    assert [c["mode"] for c in fc.calls()] == ["stream"]  # no auth status needed


def test_fetch_logged_out_is_needs_sign_in(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude(
        {"stream": {"get_usage": {"mode": "logged_out"}}, "auth_status": LOGGED_OUT_AUTH}
    )
    snap = fetch(fc, config_dir)
    assert snap.status == STATUS_NEEDS_SIGN_IN
    assert [c["mode"] for c in fc.calls()] == ["stream", "auth_status"]


def test_fetch_no_subscription_when_logged_in(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude(
        {"stream": {"get_usage": {"mode": "logged_out"}}, "auth_status": LOGGED_IN_AUTH}
    )
    assert fetch(fc, config_dir).status == STATUS_NO_SUBSCRIPTION


def test_fetch_malformed_is_source_error(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "malformed"}}})
    snap = fetch(fc, config_dir)
    assert snap.status == STATUS_SOURCE_ERROR
    assert snap.error is not None and "five_hour" in snap.error


def test_fetch_exit_early_keeps_previous_windows(fake_claude: MakeFake, config_dir: Path) -> None:
    prev = normalize(payload("ok_team.json"), profile_id="work", fetched_at=T0)
    fc = fake_claude({"stream": {"get_usage": {"mode": "exit_early"}}})
    snap = fetch(fc, config_dir, previous=prev)
    assert snap.status == STATUS_SOURCE_ERROR
    assert snap.session == prev.session and snap.fetched_at == T0 and snap.polled_at == T0


def test_fetch_without_claude_is_source_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(config_dir))
    snap = asyncio.run(fetch_snapshot(None, make_profile(config_dir), clock=FakeClock(T0)))
    assert snap.status == STATUS_SOURCE_ERROR and snap.error is not None


@pytest.mark.parametrize(
    ("raw", "error", "stderr", "auth", "expected"),
    [
        ({"rate_limits_available": True, "rate_limits": {}}, None, "", None, STATUS_OK),
        (
            {"rate_limits_available": False, "rate_limits": None},
            None,
            "",
            {"loggedIn": False},
            STATUS_NEEDS_SIGN_IN,
        ),
        (
            {"rate_limits_available": False, "rate_limits": None},
            None,
            "",
            {"loggedIn": True},
            STATUS_NO_SUBSCRIPTION,
        ),
        (
            {"rate_limits_available": False, "rate_limits": None},
            None,
            "",
            None,
            STATUS_SOURCE_ERROR,
        ),
        (None, "claude exited (rc=1)", "Error: not logged in", None, STATUS_NEEDS_SIGN_IN),
        (None, "claude exited (rc=1)", "HTTP 401", {"loggedIn": True}, STATUS_SOURCE_ERROR),
        (None, "no get_usage response within 15s", "", None, STATUS_SOURCE_ERROR),
    ],
)
def test_classify_table(
    raw: dict[str, Any] | None,
    error: str | None,
    stderr: str,
    auth: dict[str, Any] | None,
    expected: str,
) -> None:
    result = ProbeResult(raw, None if raw else "source_error", error, 0.1, stderr=stderr)
    assert classify(result, auth)[0] == expected


def test_needs_auth_check() -> None:
    assert needs_auth_check(ProbeResult({"rate_limits": None}, None, None, 0)) is True
    assert needs_auth_check(ProbeResult({"rate_limits": {}}, None, None, 0)) is False
    assert (
        needs_auth_check(ProbeResult(None, "source_error", "x", 0, stderr="Not logged in")) is True
    )
    assert needs_auth_check(ProbeResult(None, "source_error", "timeout", 0)) is False


# ------------------------------------------------------------- other wrappers


def test_agents_json_and_auth_status(fake_claude: MakeFake, config_dir: Path) -> None:
    sessions = [{"pid": 7, "kind": "interactive", "status": "busy"}, "junk"]
    fc = fake_claude({"agents": {"sessions": sessions}, "auth_status": LOGGED_OUT_AUTH})
    profile = make_profile(config_dir)
    assert asyncio.run(agents_json(str(fc.path), profile)) == [sessions[0]]
    assert asyncio.run(auth_status(str(fc.path), profile)) == {
        "loggedIn": False,
        "authMethod": "none",
    }
    assert all(c["env"]["CLAUDE_CONFIG_DIR"] == str(config_dir) for c in fc.calls())


def test_agents_json_failure(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"agents": {"stdout": "oops", "exit": 3}})
    with pytest.raises(ClaudeCliError):
        asyncio.run(agents_json(str(fc.path), make_profile(config_dir)))


def test_auth_status_garbage_is_none(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"auth_status": {"stdout": "not json"}})
    assert asyncio.run(auth_status(str(fc.path), make_profile(config_dir))) is None


def test_run_timeout_kills(fake_claude: MakeFake, config_dir: Path) -> None:
    fc = fake_claude({"print": {"delay": 10}})
    with pytest.raises(ClaudeTimeout):
        asyncio.run(
            run([str(fc.path), "-p", "hi"], env=profile_env(make_profile(config_dir)), timeout=0.3)
        )


def test_resolve_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_claude_path: Path
) -> None:
    cfg = Config.from_dict({"claude_path": str(fake_claude_path)})
    assert resolve_claude(cfg) == str(fake_claude_path)
    with pytest.raises(ClaudeNotFound):
        resolve_claude(Config.from_dict({"claude_path": str(tmp_path / "missing")}))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert resolve_claude(None) == str(exe)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ClaudeNotFound):
        resolve_claude(None)
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "claude").write_text("#!/bin/sh\n")
    (local / "claude").chmod(0o755)
    assert resolve_claude(None) == str(local / "claude")


def test_profile_env_keeps_symlinked_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    env = profile_env(
        make_profile(link), base={"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_X": "y"}
    )
    assert env == {"PATH": "/bin", "CLAUDE_CONFIG_DIR": str(link)}
