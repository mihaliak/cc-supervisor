"""A fake user terminal for launcher tests: `ccs …` runs with stdio on a PTY we control."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Term:
    """`python -m ccs <args>` on a fresh PTY (own session, so no job-control signals)."""

    def __init__(
        self,
        args: list[str],
        env: dict[str, str],
        *,
        rows: int = 30,
        cols: int = 100,
        cwd: str | None = None,
    ) -> None:
        self.master, self.slave = os.openpty()
        set_winsize(self.slave, rows, cols)
        self.attrs_before = termios.tcgetattr(self.slave)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ccs", *args],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env=env,
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
        os.set_blocking(self.master, False)
        self.out = bytearray()

    def pump(self, timeout: float = 0.05) -> None:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                return
            if not chunk:
                return
            self.out += chunk

    def wait_for(self, pred: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"condition not met; output so far: {bytes(self.out)[-2000:]!r}"
                )
            self.pump()

    def wait_output(self, needle: bytes, timeout: float = 10.0) -> None:
        self.wait_for(lambda: needle in self.out, timeout)

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def resize(self, rows: int, cols: int) -> None:
        set_winsize(self.slave, rows, cols)
        # no controlling terminal in the test session: deliver SIGWINCH ourselves
        os.kill(self.proc.pid, signal.SIGWINCH)

    def wait(self, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"launcher did not exit; output: {bytes(self.out)[-2000:]!r}")
            self.pump()
        self.pump(0.01)
        return int(self.proc.returncode)

    def attrs_now(self) -> list[Any]:
        return termios.tcgetattr(self.slave)

    def close(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(5)
        for fd in (self.master, self.slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def tui_records(log: Path) -> list[dict[str, Any]]:
    path = Path(str(log) + ".tui")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def received(log: Path) -> bytes:
    """All bytes the fake TUI read, concatenated."""
    return b"".join(bytes.fromhex(r["recv"]) for r in tui_records(log) if "recv" in r)


def winsizes(log: Path) -> list[list[int]]:
    return [r["winsize"] for r in tui_records(log) if "winsize" in r]
