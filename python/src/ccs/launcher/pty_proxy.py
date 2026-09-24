"""Transparent PTY proxy for the interactive `claude` TUI (ADR-0006, ADR-0007).

Bytes pass through unchanged in both directions, except:
- Ctrl-Z is intercepted (P00-S3): under `pty.fork` claude is a session leader in an orphaned
  process group, so its own suspend is a no-op. The proxy strips Ctrl-Z, restores the user's
  TTY and stops itself with SIGTSTP; after `fg` it re-raws and nudges the winsize so the
  fullscreen TUI repaints.
- Daemon-requested injections (`write_to_child`).
The user's TTY attrs are restored on every exit path.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time
import tty
from collections.abc import Callable
from typing import Any

from ccs.launcher.inject import PASTE_END, PASTE_START

# Ctrl-Z as legacy byte and as kitty keyboard-protocol encodings (longest first).
CTRL_Z_SEQS = (b"\x1b[122;5:1u", b"\x1b[122;5u", b"\x1a")
# Enter as kitty keyboard-protocol encodings (plain Enter is `\r`).
KITTY_ENTER = (b"\x1b[13u", b"\x1b[13;1u")
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
    """Remove Ctrl-Z encodings; returns `(rest, found)`."""
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


class SubmitScanner:
    """Detects a submit (Enter) in user input, ignoring bracketed-paste content."""

    def __init__(self) -> None:
        self.in_paste = False

    def feed(self, data: bytes) -> bool:
        submit = False
        i = 0
        while i < len(data):
            if self.in_paste:
                j = data.find(PASTE_END, i)
                if j < 0:
                    return submit
                self.in_paste = False
                i = j + len(PASTE_END)
                continue
            j = data.find(PASTE_START, i)
            seg = data[i : j if j >= 0 else len(data)]
            if b"\r" in seg or b"\n" in seg or any(k in seg for k in KITTY_ENTER):
                submit = True
            if j < 0:
                return submit
            self.in_paste = True
            i = j + len(PASTE_START)
        return submit


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
        self._scanner = SubmitScanner()
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
                tty.setraw(self.stdin_fd)
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
        self._restore_tty()
        if self.master >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = -1

    def _restore_tty(self) -> None:
        if self._saved is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(self.stdin_fd, termios.TCSAFLUSH, self._saved)

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
            return
        self.last_user_input_at = time.monotonic()
        data, suspend = strip_ctrl_z(data)
        if data:
            if self._scanner.feed(data) and self.on_user_submit is not None:
                self.on_user_submit()
            self.write_to_child(data)
        if suspend:
            self._suspend()

    def _on_master(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:  # slave closed: the child (and everything holding the pty) is gone
            assert self._loop is not None
            self._loop.remove_reader(self.master)
            self._master_on = False
            self._check_child()
            return
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
            self._to_stdout(data)

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
        self._restore_tty()
        if loop is not None and signal.SIGTSTP in self._signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGTSTP)
            self._signals.remove(signal.SIGTSTP)
        os.kill(os.getpid(), signal.SIGTSTP)  # default action: stop; returns after SIGCONT
        # resumed (`fg`)
        if self._saved is not None:
            with contextlib.suppress(termios.error):
                tty.setraw(self.stdin_fd)
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
