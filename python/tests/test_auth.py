"""`ccs auth …` (P09): parsing, Keychain name, mode choice, Terminal flow, CLI (fake claude)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import scenario, write_config

from ccs import auth
from ccs.auth import (
    AuthStatus,
    SigninFiles,
    choose_login_mode,
    command_file_text,
    keychain_service_name,
    parse_auth_status,
    wait_for_terminal_login,
    write_command_file,
)
from ccs.cli import main
from ccs.config import store
from ccs.config.models import Profile

FIXTURES = Path(__file__).parent / "fixtures" / "auth_status"


def fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def profile_of(pid: str = "work") -> Profile:
    p = store.load()[0].profile(pid)
    assert p is not None
    return p


def marker(pid: str, base: Path) -> Path:
    return base / f"profile-{pid}" / ".fake-auth"


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


# ---------------------------------------------------------------- keychain service name


def test_keychain_default_dir_has_no_suffix(tmp_home: Path) -> None:
    assert keychain_service_name("~/.claude") == "Claude Code-credentials"
    assert keychain_service_name("~/.claude/") == "Claude Code-credentials"
    assert keychain_service_name(tmp_home / ".claude") == "Claude Code-credentials"


def test_keychain_other_dir_uses_sha_prefix(tmp_home: Path) -> None:
    expected = hashlib.sha256(f"{tmp_home}/.claude-work".encode()).hexdigest()[:8]
    assert keychain_service_name("~/.claude-work") == f"Claude Code-credentials-{expected}"
    assert keychain_service_name("~/.claude-work/") == f"Claude Code-credentials-{expected}"


def test_keychain_verified_vector() -> None:
    # ADR-0003: /Users/me/.claude-work → 1e91dd84 (pure string hashing, no FS access)
    name = keychain_service_name("/Users/me/.claude-work")
    assert name == "Claude Code-credentials-1e91dd84"


def test_keychain_keeps_symlinks(tmp_home: Path) -> None:
    real = tmp_home / "real-dir"
    real.mkdir()
    link = tmp_home / "link-dir"
    link.symlink_to(real)
    expected = hashlib.sha256(str(link).encode()).hexdigest()[:8]
    assert keychain_service_name(link) == f"Claude Code-credentials-{expected}"


# ---------------------------------------------------------------- parse_auth_status


def test_parse_logged_in_fixtures() -> None:
    st = parse_auth_status(fixture("logged_in.json"))
    assert st == AuthStatus(
        True,
        account="user@example.com",
        subscription_type="max",
        auth_method="claude.ai",
        raw_keys=st.raw_keys,
    )
    assert "loggedIn" in st.raw_keys
    assert parse_auth_status(fixture("logged_in_team.json")).subscription_type == "team"


def test_parse_logged_out_fixture() -> None:
    st = parse_auth_status(fixture("logged_out.json"))
    assert not st.logged_in
    assert st.account is None and st.subscription_type is None
    assert st.auth_method == "none"
    assert st.error is None


@pytest.mark.parametrize("raw", [None, [], "x", {"authMethod": "claude.ai"}, {"loggedIn": "yes"}])
def test_parse_unknown_shapes(raw: object) -> None:
    st = parse_auth_status(raw)
    assert not st.logged_in
    assert st.error is not None


# ---------------------------------------------------------------- login mode


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "terminal", "headless", "expected"),
    [
        (True, True, False, True, "tty"),
        (True, True, False, False, "tty"),
        (True, True, True, True, "terminal"),
        (False, False, False, True, "headless"),
        (True, False, False, True, "headless"),
        (False, True, False, True, "headless"),
        (False, False, False, False, "terminal"),
        (False, False, True, True, "terminal"),
    ],
)
def test_choose_login_mode(
    stdin_tty: bool, stdout_tty: bool, terminal: bool, headless: bool, expected: str
) -> None:
    mode = choose_login_mode(
        stdin_tty=stdin_tty, stdout_tty=stdout_tty, terminal=terminal, headless_supported=headless
    )
    assert mode == expected


def test_headless_is_supported_per_s4() -> None:
    assert auth.HEADLESS_LOGIN_SUPPORTED is True


# ---------------------------------------------------------------- Terminal mode pieces


def test_command_file_text_quotes_and_exports(tmp_path: Path) -> None:
    done = tmp_path / "it's done"
    text = command_file_text(
        ["/opt/my ccs/ccs"],
        "work",
        done,
        {"XDG_CONFIG_HOME": "/x/conf", "XDG_STATE_HOME": "", "OTHER": "no"},
    )
    lines = text.splitlines()
    assert lines[0] == "#!/bin/zsh -l"
    assert "export XDG_CONFIG_HOME=/x/conf" in lines
    assert not any("XDG_STATE_HOME" in line or "OTHER" in line for line in lines)
    assert "'/opt/my ccs/ccs' auth login --profile work" in lines
    assert lines[-1].startswith("print -r -- $? > ")
    assert "it'\"'\"'s done" in lines[-1]


def test_write_command_file_modes(tmp_path: Path) -> None:
    files = SigninFiles(tmp_path / "tmp" / "signin-work.command", tmp_path / "tmp" / "x.done")
    files.done.parent.mkdir()
    files.done.write_text("0")
    write_command_file(files, "#!/bin/zsh -l\n")
    assert stat.S_IMODE(files.command.stat().st_mode) == 0o700
    assert stat.S_IMODE(files.command.parent.stat().st_mode) == 0o700
    assert not files.done.exists()  # a stale exit code from an earlier run is cleared


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _checker(results: list[AuthStatus | None]) -> tuple[list[int], Any]:
    count = [0]

    async def check() -> AuthStatus | None:
        count[0] += 1
        return results[min(count[0], len(results)) - 1]

    return count, check


def test_wait_done_file_success(tmp_path: Path) -> None:
    files = SigninFiles(tmp_path / "s.command", tmp_path / "s.done")
    files.done.write_text("0\n")
    _, check = _checker([AuthStatus(True, account="a@b")])
    status, error = asyncio.run(wait_for_terminal_login(files, check, was_logged_in=True))
    assert error is None and status is not None and status.logged_in


def test_wait_done_file_failure(tmp_path: Path) -> None:
    files = SigninFiles(tmp_path / "s.command", tmp_path / "s.done")
    files.done.write_text("1\n")
    _, check = _checker([AuthStatus(False)])
    status, error = asyncio.run(wait_for_terminal_login(files, check, was_logged_in=False))
    assert error == "sign-in in Terminal did not complete (exit 1)"
    assert status is not None and not status.logged_in


def test_wait_detects_login_of_signed_out_profile(tmp_path: Path) -> None:
    files = SigninFiles(tmp_path / "s.command", tmp_path / "s.done")
    ft = FakeTime()
    count, check = _checker([AuthStatus(False), AuthStatus(False), AuthStatus(True)])
    status, error = asyncio.run(
        wait_for_terminal_login(
            files, check, was_logged_in=False, interval=3, sleep=ft.sleep, monotonic=ft.monotonic
        )
    )
    assert error is None and status is not None and status.logged_in
    assert count[0] == 3 and ft.sleeps == [3, 3]


def test_wait_times_out(tmp_path: Path) -> None:
    files = SigninFiles(tmp_path / "s.command", tmp_path / "s.done")
    ft = FakeTime()
    # already signed in before: status alone never ends the wait (re-login to another account)
    _, check = _checker([AuthStatus(True)])
    status, error = asyncio.run(
        wait_for_terminal_login(
            files,
            check,
            was_logged_in=True,
            timeout=9,
            interval=3,
            sleep=ft.sleep,
            monotonic=ft.monotonic,
        )
    )
    assert error == "timed out after 9 s waiting for the Terminal sign-in"
    assert ft.sleeps == [3, 3, 3]
    assert status is not None


# ---------------------------------------------------------------- login()/logout() with fake claude


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    write_config(tmp_path, profiles=("work", "personal"))
    return tmp_path


def fake_claude_exe() -> str:
    return str(Path(__file__).parent / "fake_claude" / "claude")


def stateful(**login: Any) -> dict[str, Any]:
    return {
        "auth_status": {"stateful": True},
        "auth_login": {"sets_auth": True, **login},
        "auth_logout": {},
    }


def test_login_headless_signs_in(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = scenario(tmp_path, monkeypatch, stateful(stdout="Opening browser to sign in…"))
    profile = profile_of()
    outcome = asyncio.run(auth.login(None, profile, fake_claude_exe(), "headless"))
    assert outcome.error is None and outcome.logged_in
    assert outcome.status is not None and outcome.status.account == "user@example.com"
    logins = [json.loads(x) for x in log.read_text().splitlines() if '"auth_login"' in x]
    assert logins[0]["argv"] == ["auth", "login", "--claudeai"]
    assert logins[0]["env"]["CLAUDE_CONFIG_DIR"] == str(cfg_dir / "profile-work")
    assert "CLAUDECODE" not in logins[0]["env"]


def test_login_tty_mode_inherits_stdio(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = scenario(tmp_path, monkeypatch, stateful())
    outcome = asyncio.run(auth.login(None, profile_of(), fake_claude_exe(), "tty"))
    assert outcome.logged_in and outcome.mode == "tty"
    assert '"--claudeai"' in log.read_text()


def test_login_failure_reports_error(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario(tmp_path, monkeypatch, stateful(exit=1, stderr="OAuth error: access denied"))
    outcome = asyncio.run(auth.login(None, profile_of(), fake_claude_exe(), "headless"))
    assert not outcome.logged_in
    assert outcome.error == "not signed in (claude auth login exited 1): OAuth error: access denied"


def test_login_headless_timeout(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario(tmp_path, monkeypatch, stateful(delay=5))
    outcome = asyncio.run(
        auth.login(None, profile_of(), fake_claude_exe(), "headless", timeout=0.5)
    )
    assert not outcome.logged_in
    assert outcome.error == "sign-in timed out after 0.5 s"


def test_login_terminal_mode_with_stub_opener(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    seen: dict[str, Any] = {}

    def opener(path: Path) -> int:
        seen["text"] = path.read_text(encoding="utf-8")
        seen["mode"] = stat.S_IMODE(path.stat().st_mode)
        marker("work", cfg_dir).write_text("fake\n")  # the user signs in in Terminal …
        auth.signin_files("work").done.write_text("0\n")  # … and the script records exit 0
        return 0

    outcome = asyncio.run(
        auth.login(
            None,
            profile_of(),
            fake_claude_exe(),
            "terminal",
            opener=opener,
            ccs_cmd=["/abs/ccs"],
            poll_interval=0.01,
        )
    )
    assert outcome.error is None and outcome.logged_in and outcome.mode == "terminal"
    assert seen["mode"] == 0o700
    assert "/abs/ccs auth login --profile work" in seen["text"]
    files = auth.signin_files("work")
    assert not files.command.exists() and not files.done.exists()


def test_login_terminal_open_failure(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    outcome = asyncio.run(
        auth.login(None, profile_of(), fake_claude_exe(), "terminal", opener=lambda p: 1)
    )
    assert outcome.error == "could not open Terminal (open -a Terminal failed)"
    assert not auth.signin_files("work").command.exists()


def test_terminal_script_runs_ccs_login_end_to_end(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generated script (run with `zsh -f`, not a login shell) signs in and records rc 0."""
    scenario(tmp_path, monkeypatch, stateful())
    files = auth.signin_files("work")
    text = command_file_text([sys.executable, "-m", "ccs"], "work", files.done, dict(os.environ))
    write_command_file(files, text)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CCS_W"))}
    done = subprocess.run(
        ["/bin/zsh", "-f", str(files.command)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert files.done.read_text().strip() == "0"
    assert marker("work", cfg_dir).exists()


def test_logout_signs_out(cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    marker("work", cfg_dir).write_text("fake\n")
    status, error = asyncio.run(auth.logout(fake_claude_exe(), profile_of()))
    assert error is None and status is not None and not status.logged_in
    assert not marker("work", cfg_dir).exists()


def test_logout_still_signed_in(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = stateful()
    spec["auth_logout"] = {"exit": 1, "stderr": "keychain busy"}
    scenario(tmp_path, monkeypatch, spec)
    marker("work", cfg_dir).write_text("fake\n")
    status, error = asyncio.run(auth.logout(fake_claude_exe(), profile_of()))
    assert error == "still signed in (claude auth logout exited 1): keychain busy"
    assert status is not None and status.logged_in


# ---------------------------------------------------------------- CLI


def test_cli_status_logged_in_and_out(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    code, doc = run_json(capsys, "auth", "status", "--profile", "work")
    assert code == 0
    assert doc["ok"] is True and doc["logged_in"] is False and doc["account"] is None
    assert doc["config_dir"] == str(cfg_dir / "profile-work")
    assert doc["keychain_service"] == keychain_service_name(cfg_dir / "profile-work")
    marker("work", cfg_dir).write_text("fake\n")
    code, doc = run_json(capsys, "auth", "status", "--profile", "work")
    assert code == 0
    assert doc["logged_in"] is True
    assert doc["account"] == "user@example.com" and doc["subscription_type"] == "max"
    assert set(doc) == {
        "ok",
        "profile_id",
        "config_dir",
        "logged_in",
        "account",
        "subscription_type",
        "auth_method",
        "keychain_service",
        "error",
    }


def test_cli_status_human(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    assert main(["auth", "status", "--profile", "work"]) == 0
    out = capsys.readouterr().out
    assert "not signed in · run: ccs auth login --profile work" in out
    assert "keychain item: Claude Code-credentials-" in out


def test_cli_status_garbage_output_exits_1(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, {"auth_status": {"stdout": "not json at all"}})
    code, doc = run_json(capsys, "auth", "status", "--profile", "work")
    assert code == 1 and doc["ok"] is False


def test_cli_unknown_profile_exits_2(cfg_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc = run_json(capsys, "auth", "status", "--profile", "nope")
    assert code == 2 and doc["error"] == "unknown profile 'nope'"


def test_cli_missing_claude_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path, claude=tmp_path / "no-such-claude")
    code, doc = run_json(capsys, "auth", "status", "--profile", "work")
    assert code == 1 and "claude_path" in doc["error"]


def test_cli_profile_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["auth", "status"]) == 2


def test_cli_login_json_headless(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    code, doc = run_json(capsys, "auth", "login", "--profile", "work")
    assert code == 0
    assert doc == {
        "ok": True,
        "profile_id": "work",
        "mode": "headless",
        "logged_in": True,
        "account": "user@example.com",
        "subscription_type": "max",
        "daemon_refreshed": False,  # no daemon running
        "error": None,
    }


def test_cli_login_failure_exits_1(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful(exit=1))
    code, doc = run_json(capsys, "auth", "login", "--profile", "work")
    assert code == 1
    assert doc["ok"] is False and doc["logged_in"] is False
    assert doc["error"].startswith("not signed in (claude auth login exited 1)")


def test_cli_login_terminal_flag_uses_opener(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    opened: list[Path] = []

    def opener(path: Path) -> int:
        opened.append(path)
        marker("work", cfg_dir).write_text("fake\n")
        auth.signin_files("work").done.write_text("0\n")
        return 0

    monkeypatch.setattr(auth, "open_in_terminal", opener)
    code, doc = run_json(capsys, "auth", "login", "--terminal", "--profile", "work")
    assert code == 0 and doc["mode"] == "terminal" and doc["logged_in"] is True
    assert opened and opened[0].name == "signin-work.command"


def test_cli_login_tty_mode_selected(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    code = main(["auth", "login", "--profile", "work"])
    out = capsys.readouterr().out
    assert code == 0
    assert "via claude.ai" in out and "Signed in 💼 Work as user@example.com · max." in out


def test_cli_logout_json_no_prompt(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    marker("work", cfg_dir).write_text("fake\n")

    def no_input(prompt: str = "") -> str:
        raise AssertionError("--json must not prompt")

    monkeypatch.setattr("builtins.input", no_input)
    code, doc = run_json(capsys, "auth", "logout", "--profile", "work")
    assert code == 0
    assert doc == {
        "ok": True,
        "profile_id": "work",
        "logged_in": False,
        "daemon_refreshed": False,
        "error": None,
    }
    assert not marker("work", cfg_dir).exists()


@pytest.mark.parametrize(("answer", "code", "signed_out"), [("y", 0, True), ("n", 1, False)])
def test_cli_logout_prompt(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    code: int,
    signed_out: bool,
) -> None:
    log = scenario(tmp_path, monkeypatch, stateful())
    marker("work", cfg_dir).write_text("fake\n")
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    assert main(["auth", "logout", "--profile", "work"]) == code
    assert prompts and prompts[0].startswith("Log out 💼 Work (")
    assert prompts[0].endswith(")? [y/N] ")
    assert marker("work", cfg_dir).exists() is not signed_out
    called = log.exists() and '"auth_logout"' in log.read_text()
    assert called is signed_out


def test_cli_logout_eof_means_no(
    cfg_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario(tmp_path, monkeypatch, stateful())
    marker("work", cfg_dir).write_text("fake\n")

    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main(["auth", "logout", "--profile", "work"]) == 1
    assert marker("work", cfg_dir).exists()
