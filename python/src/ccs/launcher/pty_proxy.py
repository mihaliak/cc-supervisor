"""Transparent PTY proxy for the interactive `claude` TUI (ADR-0006, ADR-0007).

Bytes pass through unchanged in both directions, except:
- Ctrl-Z is intercepted (P00-S3): under `pty.fork` claude is a session leader in an orphaned
  process group, so its own suspend is a no-op. The proxy strips Ctrl-Z (outside bracketed
  pastes), resets the terminal modes claude turned on, restores the user's TTY and stops itself
  with SIGTSTP; after `fg` it re-raws, turns the modes back on and nudges the winsize so the
  fullscreen TUI repaints.
- Daemon-requested injections (`write_to_child`).
The user's TTY attrs (and the terminal modes, ADR-0022) are restored on every exit path. Raw
mode is entered with `TCSANOW` and left with `TCSADRAIN`, so typeahead is never discarded.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import pty
import re
import select
import signal
import struct
import termios
import time
import tty
from collections.abc import Callable
from typing import Any

from ccs.launcher.inject import PASTE_END, PASTE_START
from ccs.launcher.term_modes import TermModes

log = logging.getLogger(__name__)

# Ctrl-Z as legacy byte and as kitty keyboard-protocol encodings (longest first).
CTRL_Z_SEQS = (b"\x1b[122;5:1u", b"\x1b[122;5u", b"\x1a")
# Enter as kitty keyboard-protocol encodings (plain Enter is `\r`).
KITTY_ENTER = (b"\x1b[13u", b"\x1b[13;1u", b"\x1b[13;1:1u")
# Escape sequences the input filter must see whole, outside and inside a paste.
_OUTSIDE_MARKERS = (PASTE_START, *CTRL_Z_SEQS[:2], *KITTY_ENTER)
_MARKER_MAX = max(len(m) for m in (*_OUTSIDE_MARKERS, PASTE_END))
# How long a possibly split marker waits for the rest of it (a key arrives in one write).
INPUT_HOLD_S = 0.02
# Terminal reports that reach stdin without the user typing: focus in/out, mouse, replies to
# queries (`CSI ? … c|u|y`, `CSI > … c`) and OSC/DCS strings.
_REPORTS = re.compile(
    rb"\x1b\[[IO]|\x1b\[<[0-9;]*[Mm]|\x1b\[M[\x20-\xff]{3}"
    rb"|\x1b\[[?>][0-9;:$]*[A-Za-z]|\x1b[\]P][^\x07\x1b]*(?:\x07|\x1b\\)"
)
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def get_winsize(fd: int) -> tuple[int, int, int, int] | None:
    """`(rows, cols, xpix, ypix)` of a tty, or None."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return None
    rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
    return rows, cols, xpix, ypix


def set_winsize(fd: int, size: tuple[int, int, int, int]) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", *size))


def strip_ctrl_z(data: bytes) -> tuple[bytes, bool]:
    """Remove Ctrl-Z encodings (no paste awareness); returns `(rest, found)`."""
    found = False
    for seq in CTRL_Z_SEQS:
        if seq in data:
            data = data.replace(seq, b"")
            found = True
    return data, found


def exit_code_from_status(status: int) -> int:
    """Shell-style exit code: `128 + signum` for signal deaths."""
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def is_typing(data: bytes) -> bool:
    """True when user input holds more than terminal reports (focus, mouse, query replies)."""
    return bool(_REPORTS.sub(b"", data))


class InputFilter:
    r"""User input → bytes for the child, stateful across reads (ADR-0007, ADR-0022).

    Outside bracketed pastes:
    - Ctrl-Z (`0x1a`, kitty `CSI 122;5u`) is stripped and reported, so the proxy can suspend;
    - a submit is a bare `\r` or kitty Enter. `ESC \r` (Option/Shift+Enter), `\` + `\r` and
      `\n` (Ctrl-J) are claude's newlines, not submits.
    Paste content passes unchanged. A read that ends in what may be the start of a marker (paste
    start/end, kitty Ctrl-Z or Enter) keeps that part in `held` until the next read or `flush()`,
    so a marker split across reads is still seen.
    """

    def __init__(self) -> None:
        self.in_paste = False
        self.held = b""
        self._prev = 0  # the last byte passed on: an ESC or backslash before `\r` is a newline

    def feed(self, data: bytes) -> tuple[bytes, bool, bool]:
        """`(bytes for the child, submitted, ctrl_z)` for one read."""
        buf = self.held + data
        self.held = b""
        out = bytearray()
        submit = ctrl_z = False
        i = 0
        while i < len(buf):
            marker = PASTE_END if self.in_paste else PASTE_START
            j = buf.find(marker, i)
            seg = buf[i : j if j >= 0 else self._hold(buf, i)]
            if not self.in_paste:
                seg, found = strip_ctrl_z(seg)
                ctrl_z = ctrl_z or found
                submit = self._submits(seg) or submit
            self._pass(out, seg)
            if j < 0:
                break
            self._pass(out, marker)
            self.in_paste = not self.in_paste
            i = j + len(marker)
        return bytes(out), submit, ctrl_z

    def flush(self) -> bytes:
        """Stop waiting for the rest of a held marker; returns the held bytes unchanged.

        They reach the child as keys of their own, so a later `\\r` is a submit again.
        """
        data, self.held = self.held, b""
        self._prev = 0
        return data

    def _pass(self, out: bytearray, data: bytes) -> None:
        if data:
            out += data
            self._prev = data[-1]

    def _hold(self, buf: bytes, start: int) -> int:
        """Where the unsplit part of `buf` ends; a trailing partial marker goes to `held`."""
        k = buf.rfind(b"\x1b", max(start, len(buf) - _MARKER_MAX))
        if k >= 0:
            tail = buf[k:]
            markers = (PASTE_END,) if self.in_paste else _OUTSIDE_MARKERS
            if any(len(tail) < len(m) and m.startswith(tail) for m in markers):
                self.held = tail
                return k
        return len(buf)

    def _submits(self, seg: bytes) -> bool:
        if any(k in seg for k in KITTY_ENTER):
            return True
        k = seg.find(b"\r")
        while k >= 0:
            if (seg[k - 1] if k else self._prev) not in (0x1B, 0x5C):  # ESC, backslash
                return True
            k = seg.find(b"\r", k + 1)
        return False


def _write_all(fd: int, data: bytes) -> None:
    """Blocking write of all of `data` (waits if the fd is non-blocking)."""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            select.select([], [fd], [], 1.0)
            continue
        except InterruptedError:
            continue
        view = view[n:]


class PtyProxy:
    """Runs `argv` in a PTY and relays it to the user's terminal."""

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        *,
        stdin_fd: int = 0,
        stdout_fd: int = 1,
        kill_grace_s: float = 5.0,
    ) -> None:
        self.argv = argv
        self.env = env
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.kill_grace_s = kill_grace_s
        self.pid = 0
        self.master = -1
        self.last_user_input_at = 0.0
        self.on_user_submit: Callable[[], None] | None = None
        self.on_user_input: Callable[[], None] | None = None
        self.modes = TermModes()
        self._input = InputFilter()
        self._hold_timer: asyncio.TimerHandle | None = None
        self._saved: list[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit: asyncio.Future[int] | None = None
        self._out = bytearray()
        self._writer_on = False
        self._stdin_on = False
        self._master_on = False
        self._stdout_dead = False
        self._kill_timer: asyncio.TimerHandle | None = None
        self._signals: list[int] = []

    @property
    def claude_pid(self) -> int:
        return self.pid

    # ---------------------------------------------------------------- spawn

    def spawn(self) -> int:
        """Fork the child on a new PTY (sized like the user's terminal). Returns its pid."""
        size = get_winsize(self.stdin_fd) if os.isatty(self.stdin_fd) else None
        pid, master = pty.fork()
        if pid == 0:  # child
            try:
                if size is not None:
                    set_winsize(0, size)
                for sig in (signal.SIGPIPE, signal.SIGXFSZ):
                    signal.signal(sig, signal.SIG_DFL)  # Python ignores them; don't leak that
                os.execvpe(self.argv[0], self.argv, self.env)
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    os.write(2, f"ccs: cannot run {self.argv[0]}: {exc}\r\n".encode())
            os._exit(127)
        self.pid = pid
        self.master = master
        return pid

    # ---------------------------------------------------------------- run loop

    async def run(self) -> int:
        """Relay until the child exits; returns its exit code. Always restores the TTY."""
        if self.pid == 0:
            self.spawn()
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._exit = loop.create_future()
        os.set_blocking(self.master, False)
        try:
            if os.isatty(self.stdin_fd):
                self._saved = termios.tcgetattr(self.stdin_fd)
                tty.setraw(self.stdin_fd, termios.TCSANOW)  # keep typeahead
            loop.add_reader(self.stdin_fd, self._on_stdin)
            self._stdin_on = True
            loop.add_reader(self.master, self._on_master)
            self._master_on = True
            for sig in FORWARDED_SIGNALS:
                self._add_signal(sig, self._on_forward, sig)
            self._add_signal(signal.SIGWINCH, self._on_winch)
            self._add_signal(signal.SIGCHLD, self._check_child)
            self._add_signal(signal.SIGTSTP, self._suspend)
            self._check_child()
            status = await self._exit
            self._drain_master()
            return exit_code_from_status(status)
        finally:
            self._teardown()

    def _add_signal(self, sig: int, cb: Callable[..., None], *args: Any) -> None:
        assert self._loop is not None
        self._loop.add_signal_handler(sig, cb, *args)
        self._signals.append(sig)

    def _teardown(self) -> None:
        loop = self._loop
        if loop is not None:
            for sig in self._signals:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)
            self._signals.clear()
            if self._stdin_on:
                loop.remove_reader(self.stdin_fd)
                self._stdin_on = False
            if self._master_on:
                loop.remove_reader(self.master)
                self._master_on = False
            if self._writer_on:
                loop.remove_writer(self.master)
                self._writer_on = False
        if self._kill_timer is not None:
            self._kill_timer.cancel()
        self._cancel_hold()
        self._to_stdout(self.modes.reset())  # e.g. claude crashed with kitty keys still on
        self._restore_tty()
        if self.master >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = -1

    def _restore_tty(self) -> None:
        if self._saved is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self._saved)

    # ---------------------------------------------------------------- IO callbacks

    def _on_stdin(self) -> None:
        try:
            data = os.read(self.stdin_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:  # the terminal went away; the child gets SIGHUP from the shell
            assert self._loop is not None
            self._loop.remove_reader(self.stdin_fd)
            self._stdin_on = False
            self._flush_held()
            return
        self.last_user_input_at = time.monotonic()
        self._cancel_hold()
        data, submit, suspend = self._input.feed(data)
        self._pass_input(data, submit)
        if self._input.held and self._loop is not None:
            self._hold_timer = self._loop.call_later(INPUT_HOLD_S, self._flush_held)
        if suspend:
            self._suspend()

    def _pass_input(self, data: bytes, submit: bool) -> None:
        if not data:
            return
        if self.on_user_input is not None and is_typing(data):
            self.on_user_input()
        if submit and self.on_user_submit is not None:
            self.on_user_submit()
        self.write_to_child(data)

    def _flush_held(self) -> None:
        self._hold_timer = None
        self._pass_input(self._input.flush(), False)

    def _cancel_hold(self) -> None:
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None

    def _on_master(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno != errno.EIO:  # EIO is the usual end; anything else ends it too
                log.warning("pty read failed: %s", exc)
            data = b""
        if not data:  # slave closed: the child (and everything holding the pty) is gone
            assert self._loop is not None
            self._loop.remove_reader(self.master)
            self._master_on = False
            self._check_child()
            return
        self._from_child(data)

    def _from_child(self, data: bytes) -> None:
        self.modes.feed(data)
        self._to_stdout(data)

    def _to_stdout(self, data: bytes) -> None:
        if self._stdout_dead:
            return
        try:
            _write_all(self.stdout_fd, data)
        except OSError:
            self._stdout_dead = True

    def write_to_child(self, data: bytes) -> None:
        """Queue bytes for the child (user input and injections share one ordered buffer)."""
        if self.master < 0:
            return
        self._out += data
        self._flush()

    def _flush(self) -> None:
        while self._out:
            try:
                n = os.write(self.master, self._out)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._out.clear()
                break
            del self._out[:n]
        loop = self._loop
        if loop is None:
            return
        if self._out and not self._writer_on:
            loop.add_writer(self.master, self._flush)
            self._writer_on = True
        elif not self._out and self._writer_on:
            loop.remove_writer(self.master)
            self._writer_on = False

    def _drain_master(self, budget_s: float = 0.2) -> None:
        """Copy the child's last output after it exited."""
        if self.master < 0:
            return
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                ready, _, _ = select.select([self.master], [], [], 0.02)
                if not ready:
                    return
                continue
            except OSError:
                return
            if not data:
                return
            self._from_child(data)

    # ---------------------------------------------------------------- signals

    def _on_winch(self) -> None:
        size = get_winsize(self.stdin_fd)
        if size is not None and self.master >= 0:
            set_winsize(self.master, size)

    def _on_forward(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, sig)
        if sig in (signal.SIGTERM, signal.SIGHUP) and self._kill_timer is None:
            assert self._loop is not None
            self._kill_timer = self._loop.call_later(self.kill_grace_s, self._force_kill)

    def _force_kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)

    def _check_child(self) -> None:
        if self._exit is None or self._exit.done():
            return
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            self._exit.set_result(0)
            return
        if wpid == 0:
            return
        if os.WIFSTOPPED(status):
            self._suspend(child_stopped=True)  # defensive: claude stopped itself
            return
        self._exit.set_result(status)

    def _suspend(self, child_stopped: bool = False) -> None:
        """Hand the terminal back to the shell (job stops); on `fg` re-raw and repaint."""
        loop = self._loop
        self._to_stdout(self.modes.reset())
        self._restore_tty()
        if loop is not None and signal.SIGTSTP in self._signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGTSTP)
            self._signals.remove(signal.SIGTSTP)
        os.kill(os.getpid(), signal.SIGTSTP)  # default action: stop; returns after SIGCONT
        # resumed (`fg`)
        if self._saved is not None:
            with contextlib.suppress(termios.error):
                tty.setraw(self.stdin_fd, termios.TCSANOW)
        self._to_stdout(self.modes.replay())
        if loop is not None:
            self._add_signal(signal.SIGTSTP, self._suspend)
        if child_stopped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGCONT)
        self._nudge_winsize()

    def _nudge_winsize(self) -> None:
        """Resize by one row and back so the fullscreen TUI repaints."""
        size = get_winsize(self.stdin_fd)
        if size is None or self.master < 0:
            return
        rows, cols, xpix, ypix = size
        set_winsize(self.master, (max(1, rows - 1), cols, xpix, ypix))
        if self._loop is not None:
            self._loop.call_later(0.05, set_winsize, self.master, size)
        else:
            set_winsize(self.master, size)
