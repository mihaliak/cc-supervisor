"""Pure launcher helpers: Ctrl-Z, submit detection, paste, held prompt, env, statusline."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ccs.config.models import Config, Profile
from ccs.launcher import inject, prompt, session_map
from ccs.launcher.main import (
    has_settings,
    is_print_mode,
    launcher_env,
    statusline_args,
)
from ccs.launcher.pty_proxy import InputFilter, exit_code_from_status, strip_ctrl_z

NOW = datetime(2026, 9, 24, 17, 18, tzinfo=UTC)


def profile(tmp_path: Path, **extra: Any) -> Profile:
    raw = {
        "id": "work",
        "flag": "work",
        "name": "Work",
        "emoji": "💼",
        "config_dir": str(tmp_path / "cfg"),
    }
    raw.update(extra)
    return Profile.from_dict(raw)


# ---------------------------------------------------------------- Ctrl-Z / submit


@pytest.mark.parametrize(
    ("data", "rest", "found"),
    [
        (b"abc", b"abc", False),
        (b"\x1a", b"", True),
        (b"a\x1ab", b"ab", True),
        (b"\x1b[122;5u", b"", True),
        (b"x\x1b[122;5:1uy", b"xy", True),
    ],
)
def test_strip_ctrl_z(data: bytes, rest: bytes, found: bool) -> None:
    assert strip_ctrl_z(data) == (rest, found)


def submits(f: InputFilter, data: bytes) -> bool:
    return f.feed(data)[1]


def test_submit_scanner_plain_enter() -> None:
    s = InputFilter()
    assert submits(s, b"hello") is False
    assert submits(s, b"\r") is True
    assert submits(s, b"\x1b[13u") is True


def test_submit_scanner_ignores_paste_content() -> None:
    s = InputFilter()
    assert submits(s, b"\x1b[200~line1\nline2\r\x1b[201~") is False
    # a paste split across reads
    assert submits(s, b"\x1b[200~multi\n") is False
    assert submits(s, b"more\r") is False
    assert submits(s, b"\x1b[201~\r") is True


def test_exit_code_from_status() -> None:
    assert exit_code_from_status(0) == 0
    assert exit_code_from_status(3 << 8) == 3
    assert exit_code_from_status(15) == 128 + 15  # killed by SIGTERM


# ---------------------------------------------------------------- inject


def test_paste_wraps_and_strips_controls() -> None:
    assert inject.paste("go on") == b"\x1b[200~go on\x1b[201~"
    assert inject.paste("a\x1b[201~b") == b"\x1b[200~a[201~b\x1b[201~"
    assert inject.paste("x\ny") == b"\x1b[200~x\ny\x1b[201~"


class IO:
    def __init__(self, last: float) -> None:
        self.last_user_input_at = last
        self.written: list[bytes] = []

    def write_to_child(self, data: bytes) -> None:
        self.written.append(data)


def test_typing_gap_waits_until_quiet() -> None:
    clock = {"t": 100.0}

    async def fake_sleep(dt: float) -> None:
        clock["t"] += dt

    io = IO(last=99.5)
    ok = asyncio.run(
        inject.wait_for_typing_gap(
            io, gap=1.5, max_wait=30, monotonic=lambda: clock["t"], sleep=fake_sleep
        )
    )
    assert ok is True
    assert clock["t"] >= 101.0


def test_typing_gap_gives_up() -> None:
    clock = {"t": 100.0}

    async def fake_sleep(dt: float) -> None:
        clock["t"] += dt

    class Busy(IO):
        @property  # type: ignore[override]
        def last_user_input_at(self) -> float:
            return clock["t"]

        @last_user_input_at.setter
        def last_user_input_at(self, v: float) -> None:
            pass

    ok = asyncio.run(
        inject.wait_for_typing_gap(
            Busy(0), gap=1.5, max_wait=2, monotonic=lambda: clock["t"], sleep=fake_sleep
        )
    )
    assert ok is False


# ---------------------------------------------------------------- session map


def test_session_map_find() -> None:
    entries: list[dict[str, object]] = [
        {"pid": 1, "status": "idle", "sessionId": "a"},
        {"pid": 2, "status": "busy", "sessionId": "b"},
        {"pid": 3, "status": "weird"},
    ]
    assert session_map.find(entries, 2) == session_map.SessionInfo("b", "busy")
    assert session_map.find(entries, 3) == session_map.SessionInfo(None, "unknown")
    assert session_map.find(entries, 9) == session_map.SessionInfo(None, "unknown")
    assert session_map.is_busy("unknown") and not session_map.is_busy("idle")
    assert not session_map.is_known_busy("unknown") and session_map.is_known_busy("shell")


# ---------------------------------------------------------------- held prompt


def status_reply(holds: list[Any], resume_at: str | None = None) -> dict[str, Any]:
    sup: dict[str, Any] = {"state": "paused" if holds else "normal", "holds": holds}
    if resume_at:
        sup["resume_at"] = resume_at
    return {"ok": True, "profiles": [{"id": "work", "supervisor": sup}]}


def test_held_info_none_without_holds() -> None:
    assert prompt.held_info(status_reply([]), "work") is None
    assert prompt.held_info(None, "work") is None
    assert prompt.held_info(status_reply(["session"]), "other") is None


def test_held_info_objects_and_latest_resume() -> None:
    reset1 = (NOW + timedelta(minutes=42)).isoformat()
    reset2 = (NOW + timedelta(hours=2)).isoformat()
    info = prompt.held_info(
        status_reply(
            [
                {"id": "session", "resets_at": reset1},
                {"id": "weekly", "resets_at": reset2},
                {"id": "manual", "scope": "some-wrapper"},
            ]
        ),
        "work",
    )
    assert info is not None
    assert info.holds == ("session", "weekly")
    assert info.resume_at == NOW + timedelta(hours=2)


def test_held_info_string_ids_use_supervisor_resume_at() -> None:
    info = prompt.held_info(status_reply(["session"], resume_at="2026-09-24T18:00:00Z"), "work")
    assert info is not None and info.resume_at == datetime(2026, 9, 24, 18, 0, tzinfo=UTC)


def test_held_prompt_texts() -> None:
    info = prompt.HeldInfo(("session",), NOW + timedelta(minutes=42))
    text = prompt.held_prompt("Work", info, NOW, UTC)
    assert text == "Profile Work is paused until 18:00 (in 42m). Start anyway? [y/N] "
    manual = prompt.held_prompt("Work", prompt.HeldInfo(("manual",), None), NOW, UTC)
    assert manual == "Profile Work is paused (manual). Start anyway? [y/N] "


@pytest.mark.parametrize(
    ("answer", "yes"), [("y\n", True), ("YES\n", True), ("\n", False), ("n\n", False)]
)
def test_ask_yes_no(answer: str, yes: bool) -> None:
    import os

    r, w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(w, answer.encode())
    os.close(w)
    assert prompt.ask_yes_no("Q? ", r, out_w) is yes
    os.close(out_w)
    assert os.read(out_r, 100) == b"Q? "
    os.close(r)
    os.close(out_r)


# ---------------------------------------------------------------- env / statusline / modes


def test_launcher_env(tmp_path: Path) -> None:
    p = profile(tmp_path)
    env = launcher_env(p, wrapper_id="w1", base={"PATH": "/bin", "CCS_WRAPPER_ID": "old", "X": "1"})
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert env["CCS_PROFILE"] == "work"
    assert env["CCS_WRAPPER_ID"] == "w1"
    assert env["X"] == "1" and env["PATH"] == "/bin"
    assert "CCS_STATE_DIR" in env
    plain = launcher_env(p, wrapper_id=None, base={"CCS_WRAPPER_ID": "old"})
    assert "CCS_WRAPPER_ID" not in plain


def test_modes() -> None:
    assert is_print_mode(["-p", "hi"]) and is_print_mode(["--print"]) and not is_print_mode(["-c"])
    assert has_settings(["--settings", "{}"]) and has_settings(["--settings={}"])
    assert not has_settings(["-c"])


def test_statusline_args_generates_script(tmp_path: Path) -> None:
    p = profile(tmp_path)
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "ccs-statusline.py").write_text("print('x')\n")  # outdated → regenerated
    cfg = Config.from_dict({"version": 1, "default_profile": "work", "profiles": [p.to_dict()]})
    args = statusline_args(p, ["-c"], cfg)
    assert args[0] == "--settings"
    cmd = json.loads(args[1])["statusLine"]
    assert cmd["type"] == "command"
    assert cmd["command"].endswith("-S -E " + str(tmp_path / "cfg" / "ccs-statusline.py"))
    assert "print('x')" not in (tmp_path / "cfg" / "ccs-statusline.py").read_text()


def test_statusline_not_injected_with_user_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = profile(tmp_path)
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "ccs-statusline.py").write_text("")
    assert statusline_args(p, ["--settings", "{}"]) == []
    assert "--settings given; statusline not injected" in capsys.readouterr().err


def test_statusline_disabled(tmp_path: Path) -> None:
    p = profile(tmp_path, statusline={"enabled": False})
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "ccs-statusline.py").write_text("")
    assert statusline_args(p, []) == []


def test_default_config_dir_is_never_set_explicitly(tmp_home: Path) -> None:
    # an explicit CLAUDE_CONFIG_DIR=~/.claude would move claude's global state from
    # ~/.claude.json to ~/.claude/.claude.json (different trust/MCP/onboarding state)
    from ccs import claude_cli, paths

    (tmp_home / ".claude").mkdir()
    personal = Profile.from_dict(
        {
            "id": "personal",
            "flag": "personal",
            "name": "P",
            "emoji": "🏠",
            "config_dir": "~/.claude",
        }
    )
    base = {"CLAUDE_CONFIG_DIR": "/somewhere/else", "PATH": "/bin"}
    assert "CLAUDE_CONFIG_DIR" not in launcher_env(personal, wrapper_id="w", base=base)
    assert "CLAUDE_CONFIG_DIR" not in claude_cli.profile_env(personal, base)
    assert paths.is_default_claude_dir(str(tmp_home / ".claude"))
    assert not paths.is_default_claude_dir(str(tmp_home / ".claude-work"))
    work = Profile.from_dict(
        {"id": "work", "flag": "work", "name": "W", "emoji": "💼", "config_dir": "~/.claude-work"}
    )
    env = launcher_env(work, wrapper_id=None, base=base)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_home / ".claude-work")
