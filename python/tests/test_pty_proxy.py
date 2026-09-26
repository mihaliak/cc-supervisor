"""End-to-end launcher tests: `ccs --work` on our own PTY, fake claude TUI, in-process daemon."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import ThreadedDaemon, calls, scenario, short_state_dir, write_config
from pty_helpers import Term, received, winsizes

from ccs.daemon.server import Daemon
from ccs.launcher import inject

TUI_BUSY = {"banner": "FAKE-TUI ready", "status": "busy", "after_esc": "idle", "exit": 0}

Setup = Callable[[dict[str, Any]], tuple[dict[str, str], Path]]


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Setup]:
    home = tmp_path / "home"
    home.mkdir()
    with short_state_dir(monkeypatch):

        def make(interactive: dict[str, Any]) -> tuple[dict[str, str], Path]:
            spec = {
                "interactive": interactive,
                "agents": {"from_state": True},
                "stream": {"get_usage": {"mode": "ok"}},
            }
            log = scenario(tmp_path, monkeypatch, spec)
            write_config(tmp_path)
            env = dict(os.environ)
            env["HOME"] = str(home)  # no real LaunchAgent can be found
            return env, log

        yield make


def run_cmd(td: ThreadedDaemon, coro_fn: Callable[[Daemon], Any], timeout: float = 30) -> Any:
    assert td.loop is not None and td.daemon is not None
    return asyncio.run_coroutine_threadsafe(coro_fn(td.daemon), td.loop).result(timeout)


def wrappers(td: ThreadedDaemon) -> list[str]:
    return td.call(lambda: list(td.daemon.wrapper_conns) if td.daemon else [])


# ---------------------------------------------------------------- passthrough (no daemon)


def test_bytes_pass_through_and_exit_code(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready", "exit": 3})
    t = Term(["--work", "--no-supervise"], env)
    try:
        t.wait_output(b"FAKE-TUI ready")
        payload = b"hi\x03\xc3\xa9\x1b[200~pa\nste\x1b[201~"
        t.send(payload)
        t.wait_for(lambda: payload in received(log))
        t.wait_output(b"RECV ")  # child output reaches the user terminal
        t.send(b"\x04")
        assert t.wait() == 3
        assert t.attrs_now() == t.attrs_before  # user tty restored
    finally:
        t.close()
    start = next(c for c in calls(log) if c["mode"] == "interactive")
    assert start["env"]["CLAUDE_CONFIG_DIR"].endswith("profile-work")
    assert start["env"]["CCS_PROFILE"] == "work"
    assert start["env"]["CCS_WRAPPER_ID"]


def test_initial_size_and_resize(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready"})
    t = Term(["--work", "--no-supervise"], env, rows=30, cols=100)
    try:
        t.wait_output(b"FAKE-TUI ready")
        t.resize(40, 120)
        t.wait_for(lambda: [40, 120] in winsizes(log))
        t.send(b"\x04")
        assert t.wait() == 0
    finally:
        t.close()
    from pty_helpers import tui_records

    first = next(r for r in tui_records(log) if "start" in r)
    assert first["start"]["winsize"] == [30, 100]
    assert first["start"]["isatty"] is True


def test_ctrl_z_is_intercepted(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready", "stop_on": "\x1a"})
    t = Term(["--work", "--no-supervise"], env)
    try:
        t.wait_output(b"FAKE-TUI ready")
        # the test PTY is not a controlling terminal, so the launcher's SIGTSTP is discarded
        # (orphaned process group) and it resumes at once: re-raw + repaint nudge
        t.send(b"a\x1ab")
        t.wait_for(lambda: b"ab" in received(log))
        t.wait_for(lambda: [29, 100] in winsizes(log) and winsizes(log)[-1] == [30, 100])
        t.send(b"\x1b[122;5u")  # kitty encoding of Ctrl-Z
        t.send(b"c")
        t.wait_for(lambda: b"c" in received(log))
        assert b"\x1a" not in received(log)
        assert b"\x1b[122;5u" not in received(log)
        t.send(b"\x04")
        assert t.wait() == 0
    finally:
        t.close()


def test_sigterm_is_forwarded_and_tty_restored(setup: Setup) -> None:
    env, _log = setup({"banner": "FAKE-TUI ready"})
    t = Term(["--work", "--no-supervise"], env)
    try:
        t.wait_output(b"FAKE-TUI ready")
        t.proc.send_signal(signal.SIGTERM)
        assert t.wait() == 128 + signal.SIGTERM
        assert t.attrs_now() == t.attrs_before
    finally:
        t.close()


def test_unsupervised_hint_when_not_installed(setup: Setup) -> None:
    env, _log = setup({"banner": "FAKE-TUI ready"})
    t = Term(["--work"], env)
    try:
        t.wait_output(b"supervisor not installed")
        t.wait_output(b"FAKE-TUI ready")
        t.send(b"\x04")
        assert t.wait() == 0
    finally:
        t.close()


# ---------------------------------------------------------------- supervised (daemon)


def test_pause_resume_override_end_to_end(setup: Setup) -> None:
    env, log = setup(TUI_BUSY)
    events: list[dict[str, Any]] = []

    def install(daemon: Daemon) -> None:
        daemon.hooks.on_wrapper_event(lambda info, msg: events.append(msg))

    with ThreadedDaemon(extensions=[install]) as td:
        t = Term(["--work"], env)
        try:
            t.wait_output(b"FAKE-TUI ready")
            t.wait_for(lambda: len(wrappers(td)) == 1)
            wid = wrappers(td)[0]

            ack = run_cmd(
                td, lambda d: d.send_cmd(wid, "pause", {"holds": ["session"]}, timeout=25)
            )
            assert ack["result"] == "injected", ack
            assert ack["detail"]["was_busy"] is True and ack["detail"]["interrupted"] is True
            assert ack["detail"]["session_id"].startswith("fake-session-")
            assert inject.ESC in received(log)

            ack = run_cmd(
                td, lambda d: d.send_cmd(wid, "resume", {"prompt": "Continue now."}, timeout=25)
            )
            assert ack["result"] == "injected", ack
            t.wait_for(lambda: inject.paste("Continue now.") + b"\r" in received(log))

            ack = run_cmd(
                td, lambda d: d.send_cmd(wid, "pause", {"holds": ["session"]}, timeout=25)
            )
            assert ack["result"] == "skipped"  # idle now: nothing to interrupt
            t.send(b"typed\r")
            t.wait_for(lambda: any(e.get("kind") == "input_submitted_while_paused" for e in events))
            assert sum(e.get("kind") == "input_submitted_while_paused" for e in events) == 1

            t.send(b"\x04")
            assert t.wait() == 0
            t.wait_for(lambda: wrappers(td) == [] and not td.call(lambda: dict(td.daemon.sessions)))
        finally:
            t.close()
    start = next(c for c in calls(log) if c["mode"] == "interactive")
    assert start["env"]["CCS_WRAPPER_ID"] == wid
    kinds = [e.get("kind") for e in events]
    assert kinds.count("injected") >= 2


def test_pause_stops_background_agents_and_workflows_end_to_end(setup: Setup) -> None:
    """ADR-0023: busy after the ESC → the stop-agents chord, then the footer sweep."""
    env, log = setup(
        {**TUI_BUSY, "after_esc": "busy", "after_stop_agents": "busy", "after_stop_row": "idle"}
    )
    with ThreadedDaemon() as td:
        t = Term(["--work"], env)
        try:
            t.wait_output(b"FAKE-TUI ready")
            t.wait_for(lambda: len(wrappers(td)) == 1)
            wid = wrappers(td)[0]

            ack = run_cmd(
                td, lambda d: d.send_cmd(wid, "pause", {"holds": ["session"]}, timeout=40), 45
            )
            assert ack["result"] == "injected", ack
            assert ack["detail"]["was_busy"] is True and ack["detail"]["interrupted"] is True
            got = received(log)
            assert b"".join(inject.STOP_AGENTS) in got
            assert b"".join(inject.stop_row(1)) in got

            ack = run_cmd(td, lambda d: d.send_cmd(wid, "resume", {"prompt": "Go."}, timeout=25))
            assert ack["result"] == "injected", ack
            note = inject.paste(f"Go. {inject.RESTART_NOTE}")
            t.wait_for(lambda: note in received(log))

            t.send(b"\x04")
            assert t.wait() == 0
        finally:
            t.close()


def held(hold: dict[str, Any], seen: list[bool]) -> Callable[[Daemon], None]:
    def install(daemon: Daemon) -> None:
        resume = hold.get("resets_at")
        daemon.hooks.contribute_supervisor(
            lambda pid: {"state": "paused", "holds": [hold], "resume_at": resume}
        )
        daemon.hooks.on_wrapper_registered(lambda info: seen.append(info.started_overridden))

    return install


def reset_iso(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def test_held_answer_no_never_starts_claude(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready"})
    seen: list[bool] = []
    with ThreadedDaemon(extensions=[held({"id": "session", "resets_at": reset_iso(42)}, seen)]):
        t = Term(["--work"], env)
        try:
            t.wait_output(b"Start anyway? [y/N] ")
            assert b"Profile Work is paused until " in t.out
            t.send(b"n\n")
            assert t.wait() == 0
        finally:
            t.close()
    assert not [c for c in calls(log) if c["mode"] == "interactive"]
    assert seen == []


def test_held_force_starts_overridden(setup: Setup) -> None:
    env, _log = setup({"banner": "FAKE-TUI ready"})
    seen: list[bool] = []
    with ThreadedDaemon(extensions=[held({"id": "weekly", "resets_at": reset_iso(90)}, seen)]):
        t = Term(["--work", "--force"], env)
        try:
            t.wait_output(b"FAKE-TUI ready")
            t.wait_for(lambda: seen == [True])
            assert b"Start anyway" not in t.out
            t.send(b"\x04")
            assert t.wait() == 0
        finally:
            t.close()


def test_manual_hold_prompt_and_yes(setup: Setup) -> None:
    env, _log = setup({"banner": "FAKE-TUI ready"})
    seen: list[bool] = []
    with ThreadedDaemon(extensions=[held({"id": "manual", "scope": None}, seen)]):
        t = Term(["--work"], env)
        try:
            t.wait_output(b"Profile Work is paused (manual). Start anyway? [y/N] ")
            t.send(b"y\n")
            t.wait_output(b"FAKE-TUI ready")
            t.wait_for(lambda: seen == [True])
            t.send(b"\x04")
            assert t.wait() == 0
        finally:
            t.close()


# ---------------------------------------------------------------- print / non-TTY passthrough


def test_print_mode_execs_without_pty(setup: Setup) -> None:
    env, log = setup({"banner": "unused"})
    done = subprocess.run(
        [sys.executable, "-m", "ccs", "--work", "-p", "hi"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
    call = calls(log)[0]
    assert call["mode"] == "print" and call["argv"] == ["-p", "hi"]
    assert call["env"]["CLAUDE_CONFIG_DIR"].endswith("profile-work")
    assert call["env"]["CCS_PROFILE"] == "work"
    assert "CCS_WRAPPER_ID" not in call["env"]


def test_non_tty_stdout_execs_directly(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready"})
    done = subprocess.run(
        [sys.executable, "-m", "ccs", "--work", "--model", "haiku"],
        env=env,
        capture_output=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0
    call = calls(log)[0]
    assert call["mode"] == "interactive" and call["argv"] == ["--model", "haiku"]
    assert "CCS_WRAPPER_ID" not in call["env"]
