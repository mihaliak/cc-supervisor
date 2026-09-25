"""P15 launcher review fixes (ADR-0022): pause/resume/override, input filter, terminal modes,
logging, Ctrl-C before the PTY, typeahead, socket trust, Keychain name."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import (
    ThreadedDaemon,
    calls,
    make_daemon,
    scenario,
    short_state_dir,
    wait_until,
    write_config,
)
from pty_helpers import Term, received, set_winsize, tui_records

from ccs import auth, claude_cli, paths
from ccs.cli import main as cli_main
from ccs.daemon import client as daemon_client
from ccs.daemon.client import (
    AsyncDaemonClient,
    DaemonClient,
    DaemonUnavailable,
    socket_trust_error,
)
from ccs.daemon.server import Daemon
from ccs.launcher import inject
from ccs.launcher import main as launcher_main
from ccs.launcher.commands import CommandHandler, Timings
from ccs.launcher.daemon_link import DaemonLink, Registration
from ccs.launcher.pty_proxy import InputFilter, PtyProxy, is_typing
from ccs.launcher.session_map import SessionInfo
from ccs.launcher.term_modes import TermModes

FAST = Timings(
    esc_verify_s=0.01,
    resume_poll_s=0.01,
    resume_max_s=0.5,
    submit_delay_s=0.0,
    typing_gap_s=0.0,
    typing_max_s=1.0,
)
LINK_FAST = {"connect_timeout": 0.5, "start_retries_s": (0.05,), "backoff_s": (0.05, 0.1)}
PASTE_START, PASTE_END = inject.PASTE_START, inject.PASTE_END
KITTY_CTRL_Z = b"\x1b[122;5u"


class IO:
    def __init__(self) -> None:
        self.last_user_input_at = 0.0
        self.written: list[bytes] = []

    def write_to_child(self, data: bytes) -> None:
        self.written.append(data)


class Script:
    """Lookup results in order; the last one repeats."""

    def __init__(self, *statuses: str) -> None:
        self.statuses = list(statuses)

    async def __call__(self) -> SessionInfo:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SessionInfo("sid-1", status)


class Reports:
    def __init__(self, *accepted: bool) -> None:
        self.accepted = list(accepted)
        self.kinds: list[str] = []

    async def __call__(self, kind: str, detail: dict[str, Any]) -> bool:
        self.kinds.append(kind)
        return self.accepted.pop(0) if self.accepted else True


# ---------------------------------------------------------------- 1. unknown ≠ busy


def test_pause_known_busy_acks_was_busy_true_unknown_acks_none() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy", "idle"), timings=FAST)
    assert asyncio.run(h.handle({"cmd_id": "c", "type": "pause"}))[1]["was_busy"] is True
    io = IO()
    h = CommandHandler(io, Script("unknown"), timings=FAST)
    result, detail = asyncio.run(h.handle({"cmd_id": "c", "type": "pause"}))
    assert result == "injected" and io.written == [inject.ESC]  # still interrupted
    assert "was_busy" in detail and detail["was_busy"] is None


# ---------------------------------------------------------------- 2. no ESC after an override


def test_no_esc_when_user_submits_during_typing_guard() -> None:
    io = IO()
    timings = Timings(esc_verify_s=0.01, typing_gap_s=0.2, typing_max_s=2.0)
    h = CommandHandler(io, Script("busy"), Reports(), timings=timings)

    async def body() -> tuple[str, dict[str, Any]]:
        io.last_user_input_at = time.monotonic()  # the user is typing
        task = asyncio.ensure_future(h.handle({"cmd_id": "c", "type": "pause"}))
        await asyncio.sleep(0.05)
        io.last_user_input_at = time.monotonic()
        h.on_user_submit()  # … and submits: manual override
        return await task

    result, detail = asyncio.run(body())
    assert (result, detail) == ("skipped", {"reason": "overridden", "session_id": "sid-1"})
    assert io.written == []


def test_no_retry_esc_after_override() -> None:
    io = IO()
    reports = Reports()
    timings = Timings(esc_verify_s=0.2, typing_gap_s=0.0)
    h = CommandHandler(io, Script("busy", "busy", "busy"), reports, timings=timings)

    async def body() -> tuple[str, dict[str, Any]]:
        task = asyncio.ensure_future(h.handle({"cmd_id": "c", "type": "pause"}))
        await asyncio.sleep(0.05)  # the first ESC went out
        h.on_user_submit()  # the user starts their own turn
        return await task

    result, detail = asyncio.run(body())
    assert result == "skipped" and detail["reason"] == "overridden"
    assert "was_busy" not in detail
    assert io.written == [inject.ESC]  # the retry ESC would have killed the user's turn
    assert reports.kinds == ["injected", "input_submitted_while_paused"]


# ---------------------------------------------------------------- 3. draft protection


def test_resume_injects_nothing_after_typing_while_paused() -> None:
    io = IO()
    h = CommandHandler(io, Script("idle"), timings=FAST)

    async def body() -> tuple[str, dict[str, Any]]:
        await h.handle({"cmd_id": "p", "type": "pause"})
        h.on_user_input()  # a draft, no Enter
        return await h.handle({"cmd_id": "r", "type": "resume", "prompt": "Continue."})

    assert asyncio.run(body()) == ("skipped", {"reason": "user_input"})
    assert io.written == [] and h.paused is False


def test_typing_before_the_pause_does_not_block_the_prompt() -> None:
    io = IO()
    h = CommandHandler(io, Script("idle"), timings=FAST)

    async def body() -> str:
        h.on_user_input()
        await h.handle({"cmd_id": "p", "type": "pause"})  # a new pause resets the flag
        return (await h.handle({"cmd_id": "r", "type": "resume", "prompt": "go"}))[0]

    assert asyncio.run(body()) == "injected"
    assert io.written == [inject.paste("go"), inject.SUBMIT]


def test_typing_while_resume_waits_for_idle_blocks_the_prompt() -> None:
    io = IO()
    timings = Timings(resume_poll_s=0.05, resume_max_s=2.0, typing_gap_s=0.0)
    h = CommandHandler(io, Script("idle", "busy", "busy", "idle"), timings=timings)

    async def body() -> tuple[str, dict[str, Any]]:
        await h.handle({"cmd_id": "p", "type": "pause"})
        task = asyncio.ensure_future(h.handle({"cmd_id": "r", "type": "resume", "prompt": "go"}))
        await asyncio.sleep(0.02)
        h.on_user_input()
        return await task

    assert asyncio.run(body()) == ("skipped", {"reason": "user_input"})
    assert io.written == []


def test_is_typing_ignores_terminal_reports() -> None:
    for report in (
        b"\x1b[I",
        b"\x1b[O",
        b"\x1b[<64;10;5M",
        b"\x1b[<0;3;4m",
        b"\x1b[?1u",
        b"\x1b[?62;22c",
        b"\x1b]11;rgb:0000/0000/0000\x1b\\",
        b"\x1b]10;rgb:ffff/ffff/ffff\x07",
    ):
        assert is_typing(report) is False, report
    for typed in (b"a", b"\x7f", b"\x1b[A", b"\x1b[I" + b"x", b"\x1b[97u", PASTE_START + b"p"):
        assert is_typing(typed) is True, typed


def feed_reads(*chunks: bytes) -> tuple[list[str], float]:
    """Each chunk as one stdin read of a proxy (no child); `(typing callbacks, last input at)`."""
    r, w = os.pipe()
    proxy = PtyProxy(["true"], {}, stdin_fd=r)
    typed: list[str] = []
    proxy.on_user_input = lambda: typed.append("typed")

    async def body() -> None:
        proxy._loop = asyncio.get_running_loop()
        for chunk in chunks:
            os.write(w, chunk)
            proxy._on_stdin()
        await asyncio.sleep(0.1)  # held bytes flush after INPUT_HOLD_S

    try:
        asyncio.run(body())
    finally:
        os.close(r)
        os.close(w)
    return typed, proxy.last_user_input_at


@pytest.mark.parametrize(
    "chunks",
    [
        (b"\x1b[<64;1", b"0;5M"),  # SGR mouse report cut by a read boundary
        (b"\x1b[<0;3;4M\x1b[<0;3", b";4m"),
        (b"\x1b[M", b" !!"),  # X10 mouse
        (b"\x1b]11;rgb:ffff/ff", b"ff/ffff\x1b\\"),  # OSC color reply
        (b"\x1b]11;rgb:ffff/ffff/ffff\x1b", b"\\"),  # … cut inside its ST
        (b"\x1b[?62;2", b"2c"),  # device attributes reply
        (b"\x1b[I",),  # focus
    ],
)
def test_reports_split_across_reads_are_not_typing(chunks: tuple[bytes, ...]) -> None:
    typed, last_input = feed_reads(*chunks)
    assert typed == [] and last_input == 0.0  # the typing guard ignores them too


@pytest.mark.parametrize(
    "chunks",
    [
        (b"a",),
        (b"\x1b",),  # a lone Esc key (flushed after the hold)
        (b"\x1b[<64;1", b"0;5Mx"),  # a key after a split report
        (b"\x1b[<6", b"zz"),  # never became a report
        (b"\x1b]",),  # Alt+] alone: counted once the hold ends
    ],
)
def test_keys_still_count_as_typing(chunks: tuple[bytes, ...]) -> None:
    typed, last_input = feed_reads(*chunks)
    assert typed == ["typed"] and last_input > 0


# ---------------------------------------------------------------- 6. submit detection


@pytest.mark.parametrize(
    ("data", "submit"),
    [
        (b"\r", True),
        (b"abc\r", True),
        (b"\x1b\r", False),  # Option/Shift+Enter
        (b"\\\r", False),  # backslash + Enter
        (b"\n", False),  # Ctrl-J
        (b"line\\\rmore\r", True),
        (b"\x1b[13u", True),
        (b"\x1b[13;1:1u", True),
        (b"\x1b[13;2u", False),  # kitty Shift+Enter
        (PASTE_START + b"a\rb" + PASTE_END, False),
        (PASTE_START + b"a" + PASTE_END + b"\r", True),
    ],
)
def test_submit_is_a_bare_cr_outside_pastes(data: bytes, submit: bool) -> None:
    f = InputFilter()
    out, got, ctrl_z = f.feed(data)
    assert got is submit
    assert out + f.flush() == data and ctrl_z is False


def test_submit_uses_the_previous_read() -> None:
    f = InputFilter()
    assert f.feed(b"abc\\")[1] is False
    assert f.feed(b"\r")[1] is False  # `\` then Enter, typed as two keys
    f = InputFilter()
    assert f.feed(b"\x1b") == (b"", False, False)  # held: may start a sequence
    assert f.feed(b"\r") == (b"\x1b\r", False, False)  # Option+Enter split across reads
    f = InputFilter()
    f.feed(b"\x1b")
    assert f.flush() == b"\x1b"  # a real ESC key; the next Enter submits
    assert f.feed(b"\r")[1] is True


@pytest.mark.parametrize("cut", range(1, len(PASTE_START)))
def test_paste_start_split_across_reads(cut: int) -> None:
    f = InputFilter()
    a = f.feed(b"x" + PASTE_START[:cut])
    b = f.feed(PASTE_START[cut:] + b"one\r\x1atwo" + PASTE_END)
    assert a[0] + b[0] == b"x" + PASTE_START + b"one\r\x1atwo" + PASTE_END
    assert not (a[1] or b[1]) and not (a[2] or b[2])


@pytest.mark.parametrize("cut", range(1, len(PASTE_END)))
def test_paste_end_split_across_reads(cut: int) -> None:
    f = InputFilter()
    a = f.feed(PASTE_START + b"one\r" + PASTE_END[:cut])
    b = f.feed(PASTE_END[cut:] + b"\r")
    assert a[0] + b[0] == PASTE_START + b"one\r" + PASTE_END + b"\r"
    assert a[1] is False and b[1] is True


# ---------------------------------------------------------------- 10. Ctrl-Z only outside pastes


def test_ctrl_z_kept_inside_pastes() -> None:
    data = b"a\x1a" + PASTE_START + b"p\x1aq" + KITTY_CTRL_Z + PASTE_END + b"b" + KITTY_CTRL_Z
    out, _submit, ctrl_z = InputFilter().feed(data)
    assert out == b"a" + PASTE_START + b"p\x1aq" + KITTY_CTRL_Z + PASTE_END + b"b"
    assert ctrl_z is True
    only_pasted = PASTE_START + b"\x1a" + PASTE_END
    assert InputFilter().feed(only_pasted) == (only_pasted, False, False)


@pytest.mark.parametrize("seq", [KITTY_CTRL_Z, b"\x1b[122;5:1u"])
def test_kitty_ctrl_z_split_across_reads(seq: bytes) -> None:
    for cut in range(1, len(seq)):
        f = InputFilter()
        a = f.feed(b"x" + seq[:cut])
        b = f.feed(seq[cut:] + b"y")
        assert a[0] + b[0] == b"xy", cut
        assert a[2] or b[2], cut


# ---------------------------------------------------------------- 4. terminal modes


def modes(*chunks: bytes) -> TermModes:
    m = TermModes()
    for chunk in chunks:
        m.feed(chunk)
    return m


def test_modes_untouched_terminal_needs_nothing() -> None:
    m = modes(b"hello \x1b[31mred\x1b[0m \x1b[?u \x1b[?2004$p \x1b[>1;2m")
    assert m.reset() == b"" and m.replay() == b""
    assert modes(b"\x1b[?2004h\x1b[>1u\x1b[?2004l\x1b[<u").reset() == b""


def test_modes_dec_private_reset_and_replay() -> None:
    m = modes(b"\x1b[?1000;1006h\x1b[?2004h\x1b[?1004h\x1b[?25l\x1b[?1002h\x1b[?1002l")
    assert m.reset() == b"\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[?1004l\x1b[?25h"
    assert m.replay() == b"\x1b[?1000h\x1b[?1006h\x1b[?2004h\x1b[?1004h\x1b[?25l"
    assert m.reset() == b"\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[?1004l\x1b[?25h"  # no state change


def test_modes_kitty_stack() -> None:
    assert modes(b"\x1b[>1u").reset() == b"\x1b[<1u"
    nested = modes(b"\x1b[=1;1u\x1b[>3u\x1b[>5u")
    assert nested.reset() == b"\x1b[<2u\x1b[=0;1u"
    assert nested.replay() == b"\x1b[=1;1u\x1b[>3u\x1b[>5u"
    assert modes(b"\x1b[>1u\x1b[=4;2u").replay() == b"\x1b[>5u"  # add bits
    assert modes(b"\x1b[>5u\x1b[=1;3u").replay() == b"\x1b[>4u"  # remove bits
    assert modes(b"\x1b[=1u\x1b[>3u\x1b[<9u").reset() == b""  # over-popping resets all


def test_modes_kitty_stacks_are_per_screen() -> None:
    alt = modes(b"\x1b[?1049h\x1b[>1u")
    assert alt.reset() == b"\x1b[<1u\x1b[?1049l"
    assert alt.replay() == b"\x1b[?1049h\x1b[>1u"
    both = modes(b"\x1b[>2u\x1b[?1049h\x1b[>1u\x1b[?25l")
    assert both.reset() == b"\x1b[<1u\x1b[?25h\x1b[?1049l\x1b[<1u"
    assert both.replay() == b"\x1b[>2u\x1b[?1049h\x1b[>1u\x1b[?25l"


def test_modes_modify_other_keys_and_ris() -> None:
    m = modes(b"\x1b[>4;2m")
    assert m.reset() == b"\x1b[>4m" and m.replay() == b"\x1b[>4;2m"
    assert modes(b"\x1b[>4;2m\x1b[>4;0m").reset() == b""
    assert modes(b"\x1b[?2004h\x1b[>1u\x1b[>4;1m\x1bc").reset() == b""  # full reset


def test_modes_sequences_split_across_reads() -> None:
    stream = b"ab\x1b[?2004h cd \x1b[>1u ef \x1b[>4;2m \x1b[?1000;1006h"
    whole = modes(stream)
    for cut in range(1, len(stream)):
        split = modes(stream[:cut], stream[cut:])
        assert (split.reset(), split.replay()) == (whole.reset(), whole.replay()), cut


# ---------------------------------------------------------------- in-process proxy on a test PTY


class UserTty:
    """A PTY standing in for the user's terminal; a thread reads what the proxy writes.

    Like a real terminal it reads on its own: restoring the TTY with `TCSADRAIN` waits for it.
    """

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        set_winsize(self.slave, 30, 100)
        self.out = bytearray()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.out += chunk

    def close(self) -> None:
        time.sleep(0.1)  # let the reader catch the last bytes
        for fd in (self.slave, self.master):
            with contextlib.suppress(OSError):
                os.close(fd)
        if self._thread.is_alive():
            self._thread.join(2)


def run_proxy(
    proxy: PtyProxy, tty: UserTty, sends: list[tuple[float, bytes]], timeout: float = 15
) -> int:
    proxy.spawn()  # fork before the reader thread exists
    tty.start()

    async def main() -> int:
        loop = asyncio.get_running_loop()
        for delay, data in sends:
            loop.call_later(delay, os.write, tty.master, data)
        return await asyncio.wait_for(proxy.run(), timeout)

    return asyncio.run(main())


def test_typeahead_before_raw_mode_reaches_claude() -> None:
    tty = UserTty()
    try:
        os.write(tty.master, b"hello")  # typed while ccs was still starting
        time.sleep(0.1)
        child = [
            sys.executable,
            "-c",
            "import os,select,termios,tty; tty.setraw(0, termios.TCSANOW); "
            "r,_,_=select.select([0],[],[],3); "
            "os.write(1, b'GOT:' + (os.read(0, 100) if r else b'') + b':END')",
        ]
        proxy = PtyProxy(child, dict(os.environ), stdin_fd=tty.slave, stdout_fd=tty.slave)
        assert run_proxy(proxy, tty, []) == 0
    finally:
        tty.close()
    assert b"GOT:hello:END" in tty.out


def test_lone_esc_key_is_delivered_without_a_next_key(
    fake_claude: Callable[[dict[str, Any]], Any],
) -> None:
    fc = fake_claude({"interactive": {"banner": "FAKE-TUI ready"}})
    tty = UserTty()
    try:
        proxy = PtyProxy([str(fc.path)], fc.env, stdin_fd=tty.slave, stdout_fd=tty.slave)
        inputs: list[str] = []
        proxy.on_user_input = lambda: inputs.append("typed")
        code = run_proxy(proxy, tty, [(0.8, b"\x1b[I"), (1.2, b"\x1b"), (2.5, b"\x04")])
    finally:
        tty.close()
    assert code == 0
    chunks = [bytes.fromhex(r["recv"]) for r in tui_records(fc.log_path) if "recv" in r]
    assert chunks == [b"\x1b[I", b"\x1b", b"\x04"]  # the ESC was not held back until ^D
    assert inputs == ["typed", "typed"]  # the focus report is not typing


def test_child_crash_resets_terminal_modes() -> None:
    child = [
        sys.executable,
        "-c",
        "import os,signal; os.write(1, b'\\x1b[?2004h\\x1b[>1u\\x1b[?1049h\\x1b[?25l'); "
        "os.kill(os.getpid(), signal.SIGKILL)",
    ]
    tty = UserTty()
    try:
        proxy = PtyProxy(child, dict(os.environ), stdin_fd=tty.slave, stdout_fd=tty.slave)
        code = run_proxy(proxy, tty, [])
    finally:
        tty.close()
    assert code == 128 + signal.SIGKILL
    # kitty flags were pushed on the main screen: popped after leaving the alternate one
    assert bytes(tty.out).endswith(b"\x1b[?2004l\x1b[?25h\x1b[?1049l\x1b[<1u")


def test_stuck_terminal_never_blocks_signals() -> None:
    """A terminal that stops reading must not freeze the loop: SIGTERM still reaches claude,
    and the TTY is restored without waiting for the terminal forever."""
    child = [sys.executable, "-c", "import os\nb = b'x' * 65536\nwhile True: os.write(1, b)"]
    tty = UserTty()  # its reader starts only when the watchdog below gives up
    released: list[float] = []

    def release() -> None:
        released.append(time.monotonic())
        tty.start()

    watchdog = threading.Timer(8.0, release)
    killer = threading.Timer(0.7, os.kill, (os.getpid(), signal.SIGTERM))
    try:
        attrs = termios.tcgetattr(tty.slave)
        proxy = PtyProxy(child, dict(os.environ), stdin_fd=tty.slave, stdout_fd=tty.slave)
        proxy.spawn()

        async def main() -> int:
            watchdog.start()
            killer.start()
            return await proxy.run()

        started = time.monotonic()
        code = asyncio.run(main())
        took = time.monotonic() - started
        now = termios.tcgetattr(tty.slave)
        now[3] &= ~termios.PENDIN  # the kernel's own "retype pending input" marker
        assert now == attrs  # restored even though nobody reads
    finally:
        killer.cancel()
        watchdog.cancel()
        tty.close()
    assert code == 128 + signal.SIGTERM
    assert not released, f"the proxy only finished once the terminal read again ({took:.1f} s)"
    assert took < 5


def test_slow_terminal_gets_all_output_in_order() -> None:
    """Backpressure pauses claude's output while the terminal doesn't read; nothing is lost
    or reordered once it reads again."""
    child = [
        sys.executable,
        "-c",
        "import os\nfor i in range(0, 200000, 1000):\n"
        "    os.write(1, b''.join(b'%07d,' % j for j in range(i, i + 1000)))",
    ]
    tty = UserTty()
    starter = threading.Timer(1.0, tty.start)
    try:
        proxy = PtyProxy(child, dict(os.environ), stdin_fd=tty.slave, stdout_fd=tty.slave)
        proxy.spawn()

        async def main() -> int:
            starter.start()
            return await asyncio.wait_for(proxy.run(), 30)

        assert asyncio.run(main()) == 0
    finally:
        starter.cancel()
        tty.close()
    expected = b"".join(b"%07d," % j for j in range(200000))
    assert bytes(tty.out).startswith(expected)


def test_master_read_error_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proxy = PtyProxy(["true"], {})
    fd = os.open(tmp_path, os.O_RDONLY)  # reading a directory fails with EISDIR, not EIO
    loop = asyncio.new_event_loop()
    try:
        proxy._loop, proxy.master, proxy._master_on = loop, fd, True
        with caplog.at_level(logging.WARNING, logger="ccs.launcher.pty_proxy"):
            proxy._on_master()  # used to raise into the loop's exception handler, again and again
        assert proxy._master_on is False
        assert "pty read failed" in caplog.text
    finally:
        loop.close()
        os.close(fd)


# ---------------------------------------------------------------- end to end (fake TUI, own PTY)

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


def wrappers(td: ThreadedDaemon) -> list[str]:
    return td.call(lambda: list(td.daemon.wrapper_conns) if td.daemon else [])


def send_cmd(td: ThreadedDaemon, wid: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    assert td.loop is not None and td.daemon is not None
    daemon = td.daemon
    fut = asyncio.run_coroutine_threadsafe(daemon.send_cmd(wid, kind, payload, timeout=25), td.loop)
    result: dict[str, Any] = fut.result(30)
    return result


MODES_ON = "\x1b[?2004h\x1b[>1u\x1b[?1004h"
MODES_OFF = b"\x1b[<1u\x1b[?2004l\x1b[?1004l"


def test_ctrl_z_resets_and_replays_terminal_modes(setup: Setup) -> None:
    env, _log = setup({"banner": MODES_ON + "FAKE-TUI ready"})
    t = Term(["--work", "--no-supervise"], env)
    try:
        t.wait_output(b"FAKE-TUI ready")
        # the test PTY is no controlling terminal: SIGTSTP is discarded and ccs resumes at once
        t.send(b"\x1a")
        t.wait_output(MODES_OFF + b"\x1b[>1u\x1b[?2004h\x1b[?1004h")
        t.send(b"\x04")
        assert t.wait() == 0
    finally:
        t.close()
    assert bytes(t.out).endswith(MODES_OFF)  # the fake TUI exits without resetting them


def test_modes_reset_when_claude_is_killed(setup: Setup) -> None:
    env, _log = setup({"banner": MODES_ON + "FAKE-TUI ready"})
    t = Term(["--work", "--no-supervise"], env)
    try:
        t.wait_output(b"FAKE-TUI ready")
        t.proc.send_signal(signal.SIGTERM)
        assert t.wait() == 128 + signal.SIGTERM
    finally:
        t.close()
    assert bytes(t.out).endswith(MODES_OFF)


def test_draft_while_paused_blocks_the_resume_prompt(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready", "status": "idle"})
    with ThreadedDaemon() as td:
        t = Term(["--work"], env)
        try:
            t.wait_output(b"FAKE-TUI ready")
            t.wait_for(lambda: len(wrappers(td)) == 1)
            wid = wrappers(td)[0]
            assert send_cmd(td, wid, "pause", {"holds": ["session"]})["result"] == "skipped"
            t.send(b"\x1b[I")  # focus report: not typing
            t.wait_for(lambda: b"\x1b[I" in received(log))
            ack = send_cmd(td, wid, "resume", {"prompt": "Continue now."})
            assert ack["result"] == "injected", ack
            t.wait_for(lambda: inject.paste("Continue now.") in received(log))

            assert send_cmd(td, wid, "pause", {"holds": ["session"]})["result"] == "skipped"
            t.send(b"half a thought")  # a draft, no Enter
            t.wait_for(lambda: b"half a thought" in received(log))
            ack = send_cmd(td, wid, "resume", {"prompt": "Second prompt."})
            assert (ack["result"], ack["detail"]) == ("skipped", {"reason": "user_input"})
            t.send(b"\x04")
            assert t.wait() == 0
        finally:
            t.close()
    assert inject.paste("Second prompt.") not in received(log)


def test_launcher_logs_go_to_the_state_dir_not_the_tty(
    setup: Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _log = setup({"banner": "FAKE-TUI ready"})
    # the launcher's config has a profile the daemon never heard of: register is refused
    # and the launcher logs a warning (it used to reach the TTY through logging.lastResort)
    other = tmp_path / "other-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(other))
    write_config(tmp_path, profiles=("solo",))
    env["XDG_CONFIG_HOME"] = str(other)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "unused"))
    launcher_log = paths.log_dir() / launcher_main.LAUNCHER_LOG
    with ThreadedDaemon():
        t = Term(["--solo"], env)
        try:
            t.wait_output(b"FAKE-TUI ready")
            t.wait_for(
                lambda: launcher_log.exists() and "refused" in launcher_log.read_text("utf-8")
            )
            t.send(b"\x04")
            assert t.wait() == 0
        finally:
            t.close()
    assert b"refused" not in t.out and b"Traceback" not in t.out


# ---------------------------------------------------------------- 5. logging / exec errors


def test_file_logging_routes_records_and_loop_errors_to_the_log(
    tmp_xdg: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    last_resort = logging.lastResort
    with launcher_main.file_logging():
        assert logging.lastResort is None and logging.raiseExceptions is False
        logging.getLogger("ccs.test").warning("hello-launcher-log")

        async def boom() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(launcher_main._log_loop_exception)
            loop.call_soon(lambda: 1 / 0)
            await asyncio.sleep(0.02)

        asyncio.run(boom())
    assert logging.lastResort is last_resort and logging.raiseExceptions is True
    text = (paths.log_dir() / launcher_main.LAUNCHER_LOG).read_text("utf-8")
    assert "hello-launcher-log" in text and "ZeroDivisionError" in text
    captured = capfd.readouterr()
    assert captured.err == "" and captured.out == ""


def test_file_logging_without_a_writable_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("CCS_STATE_DIR", str(blocker))
    assert isinstance(launcher_main._log_handler(), logging.NullHandler)


def bad_interpreter(tmp_path: Path) -> Path:
    """An executable whose `#!` interpreter isn't executable: exec fails with EACCES."""
    interp = tmp_path / "not-executable"
    interp.write_text("x\n")
    exe = tmp_path / "claude"
    exe.write_text(f"#!{interp}\n")
    exe.chmod(0o755)
    return exe


def test_run_maps_every_exec_oserror(tmp_path: Path) -> None:
    exe = bad_interpreter(tmp_path)
    with pytest.raises(claude_cli.ClaudeNotFound, match="cannot execute"):
        asyncio.run(claude_cli.run([str(exe), "agents"], env={}, timeout=5))


def test_auth_status_with_unrunnable_claude_has_no_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path, claude=bad_interpreter(tmp_path))
    assert cli_main(["auth", "status", "--profile", "work"]) == 1
    err = capsys.readouterr().err
    assert "could not run `claude auth status`" in err and "Traceback" not in err


# ---------------------------------------------------------------- 7. override report delivery


def test_undelivered_override_is_resent_after_reregister() -> None:
    reports = Reports(False, True)

    async def body() -> CommandHandler:
        h = CommandHandler(IO(), Script("idle"), reports, timings=FAST)
        h.apply_supervision({"supervision": {"state": "paused"}})
        h.on_user_submit()  # link down: not accepted
        await asyncio.sleep(0.02)
        assert h.override_reported is False
        h.apply_supervision({"supervision": {"state": "paused"}})  # re-registered, still paused
        await asyncio.sleep(0.02)
        assert h.override_reported is True
        h.apply_supervision({"supervision": {"state": "paused"}})
        h.on_user_submit()
        await asyncio.sleep(0.02)
        return h

    asyncio.run(body())
    assert reports.kinds == ["input_submitted_while_paused"] * 2


def test_override_the_daemon_already_has_is_not_resent() -> None:
    reports = Reports(False)

    async def body() -> None:
        h = CommandHandler(IO(), Script("idle"), reports, timings=FAST)
        h.apply_supervision({"supervision": {"state": "paused"}})
        h.on_user_submit()
        await asyncio.sleep(0.02)
        h.apply_supervision({"supervision": {"state": "overridden"}})
        assert h.override_reported is True and h.paused is False
        await asyncio.sleep(0.02)

    asyncio.run(body())
    assert reports.kinds == ["input_submitted_while_paused"]


@pytest.fixture
def link_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_state_dir(monkeypatch) as d:
        scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        yield d


def reg(wrapper_id: str) -> Registration:
    return Registration(
        wrapper_id=wrapper_id,
        profile_id="work",
        wrapper_pid=111,
        claude_pid=222,
        cwd="/tmp",
        started_overridden=False,
    )


def test_override_made_while_the_daemon_is_down_reaches_the_next_daemon(
    link_env: Path,
) -> None:
    events: list[str] = []

    def paused_session(daemon: Daemon) -> None:
        daemon.hooks.on_wrapper_registered(lambda info: {"supervision": {"state": "paused"}})
        daemon.hooks.on_wrapper_event(lambda info, msg: events.append(str(msg.get("kind"))))

    async def body() -> None:
        daemon = make_daemon(extensions=[paused_session])
        await daemon.start()
        link = DaemonLink("work", "w-p15", **LINK_FAST)  # type: ignore[arg-type]
        assert await link.wrapper_event("x", {}) is False  # not registered yet
        assert await link.connect()
        h = CommandHandler(IO(), Script("idle"), link.wrapper_event, timings=FAST)
        link.start(reg("w-p15"), h.handle, h.apply_supervision)
        await wait_until(lambda: link.registered and h.paused)
        await daemon.stop()  # the daemon dies mid-pause
        await wait_until(lambda: not link.registered)
        h.on_user_submit()
        await asyncio.sleep(0.1)
        assert h.submitted and h.override_reported is False
        daemon2 = make_daemon(extensions=[paused_session])
        await daemon2.start()
        try:
            await wait_until(lambda: h.override_reported, timeout=5)
        finally:
            await link.close(0)
            await daemon2.stop()

    asyncio.run(body())
    assert events == ["input_submitted_while_paused"]


# ---------------------------------------------------------------- 8. Ctrl-C before the PTY


class CttyTerm(Term):
    """`Term`, but the PTY is the launcher's controlling terminal, so ^C sends SIGINT."""

    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        self.master, self.slave = os.openpty()
        set_winsize(self.slave, 30, 100)
        self.attrs_before = termios.tcgetattr(self.slave)
        boot = (
            "import fcntl, os, sys, termios; os.setsid(); "
            "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
            "os.execv(sys.executable, [sys.executable, '-m', 'ccs', *sys.argv[1:]])"
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-c", boot, *args],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env=env,
            close_fds=True,
        )
        os.set_blocking(self.master, False)
        self.out = bytearray()


def held_session(daemon: Daemon) -> None:
    resume = (datetime.now(UTC) + timedelta(minutes=42)).isoformat()
    hold = {"id": "session", "resets_at": resume}
    daemon.hooks.contribute_supervisor(
        lambda pid: {"state": "paused", "holds": [hold], "resume_at": resume}
    )


def test_ctrl_c_at_the_start_prompt_exits_130_at_once(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready"})
    with ThreadedDaemon(extensions=[held_session]):
        t = CttyTerm(["--work"], env)
        try:
            t.wait_output(b"Start anyway? [y/N] ")
            t.send(b"\x03")  # one press
            assert t.wait(5) == 130
        finally:
            t.close()
    assert b"Traceback" not in t.out
    assert not calls(log, "interactive")


def test_ctrl_c_while_connecting_exits_130_without_claude(setup: Setup) -> None:
    env, log = setup({"banner": "FAKE-TUI ready"})
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # a daemon that never answers
    srv.bind(str(paths.daemon_sock()))
    srv.listen(4)
    srv.settimeout(15)
    try:
        t = CttyTerm(["--work"], env)
        try:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(5)
                assert b"hello" in conn.recv(4096)  # the launcher waits for the reply now
                t.send(b"\x03")
                assert t.wait(5) == 130
        finally:
            t.close()
    finally:
        srv.close()
    assert b"Traceback" not in t.out
    assert not calls(log, "interactive")


def test_ctrl_c_while_loading_the_config_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    from ccs import cli

    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.store, "ensure_config", interrupted)
    assert cli_main(["--work"]) == 130


# ---------------------------------------------------------------- 11. socket trust


@pytest.fixture
def sockdir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="ccs-s-", dir="/tmp"))  # short: sun_path limit
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def listening(path: Path) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    return srv


def test_socket_trust(sockdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock = sockdir / "daemon.sock"
    assert socket_trust_error(sock) is None  # missing: connecting reports it
    with contextlib.closing(listening(sock)):
        assert socket_trust_error(sock) is None
        sockdir.chmod(0o770)
        assert "group- or world-writable" in (socket_trust_error(sock) or "")
        sockdir.chmod(0o700)
        uid = os.getuid()
        monkeypatch.setattr(daemon_client.os, "getuid", lambda: uid + 1)
        assert "not owned by the current user" in (socket_trust_error(sock) or "")


def test_socket_symlink_or_file_is_refused(sockdir: Path) -> None:
    real = sockdir / "real.sock"
    with contextlib.closing(listening(real)):
        link = sockdir / "daemon.sock"
        link.symlink_to(real)
        assert "not a socket" in (socket_trust_error(link) or "")
    plain = sockdir / "plain.sock"
    plain.write_text("")
    assert "not a socket" in (socket_trust_error(plain) or "")


def test_clients_refuse_an_untrusted_socket(
    sockdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sock = sockdir / "daemon.sock"
    with contextlib.closing(listening(sock)):
        sockdir.chmod(0o777)
        with caplog.at_level(logging.WARNING, logger="ccs.daemon.client"):
            with pytest.raises(DaemonUnavailable, match="refusing"):
                DaemonClient(sock, timeout=0.5).connect()

            async def async_connect() -> None:
                await AsyncDaemonClient(sock, timeout=0.5).connect()

            with pytest.raises(DaemonUnavailable, match="refusing"):
                asyncio.run(async_connect())
        assert "refusing the daemon socket" in caplog.text

        class NotInstalled:
            def is_installed(self) -> bool:
                return False

            def start(self) -> None:
                raise AssertionError("not installed")

        printed: list[str] = []

        async def link_connect() -> bool:
            link = DaemonLink(
                "work", "w-1", sock_path=sock, launchd=NotInstalled(), err=printed.append
            )
            return await link.connect()

        assert asyncio.run(link_connect()) is False  # a listening daemon, but not trusted
        assert printed and "running unsupervised" in printed[0]
        sockdir.chmod(0o700)


# ---------------------------------------------------------------- 12. Keychain service name


def test_keychain_symlink_to_default_dir_uses_default_service(tmp_home: Path) -> None:
    (tmp_home / ".claude").mkdir()
    link = tmp_home / "claude-personal"
    link.symlink_to(tmp_home / ".claude")
    # the launcher unsets CLAUDE_CONFIG_DIR for it, so Claude Code uses the default item
    assert auth.keychain_service_name(link) == auth.DEFAULT_KEYCHAIN_SERVICE
    assert auth.keychain_service_name("~/claude-personal") == auth.DEFAULT_KEYCHAIN_SERVICE
    assert paths.is_default_claude_dir(str(link))
