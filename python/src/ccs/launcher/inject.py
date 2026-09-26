"""Keystroke injection builders and the typing guard (ADR-0007, ADR-0023).

Only ESC, the stop-background-work keys (ADR-0023) and the resume prompt text are ever
injected. Never slash commands: P00-S3 showed `/model <x>` persists `model` into the profile's
settings.json.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

ESC = b"\x1b"  # interrupts a busy turn (P00-S3: stopped generation in 0.21 s)
SUBMIT = b"\r"  # written as a separate write shortly after the paste
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"

# ADR-0023: ESC never stops background agents or workflows (claude skips them on purpose).
# `chat:killAgents` (ctrl+x ctrl+k, pressed twice) stops background agents; each key is a
# separate write so the chord parser sees single keys.
STOP_AGENTS = (b"\x18", b"\x0b", b"\x18", b"\x0b")
# Workflows are only stoppable from the prompt footer: Down selects a row, `x` stops a running
# one, Backspace dismisses the stopped row (clearing the selection) or, when `x` fell through
# into the empty prompt, deletes it again.
DOWN = b"\x1b[B"
STOP_ROW = b"x"
BACKSPACE = b"\x7f"
RESTART_NOTE = (
    "Background agents or workflows stopped at the pause were stopped by the usage pause, "
    "not by the user: start again any that had not finished."
)


def stop_row(depth: int) -> tuple[bytes, ...]:
    """Keys that select footer row `depth` (1 = first), stop it and clear the selection."""
    return (DOWN,) * depth + (STOP_ROW, BACKSPACE)


class ChildIO(Protocol):
    """What the command handler needs from the PTY proxy."""

    @property
    def last_user_input_at(self) -> float: ...

    def write_to_child(self, data: bytes) -> None: ...


def paste(text: str) -> bytes:
    """`text` as a bracketed paste (control bytes stripped so it cannot escape the paste)."""
    clean = "".join(ch for ch in text if ch in "\t\n" or ord(ch) >= 0x20).replace("\x7f", "")
    return PASTE_START + clean.encode("utf-8") + PASTE_END


async def wait_for_typing_gap(
    io: ChildIO,
    *,
    gap: float = 1.5,
    max_wait: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    step: float = 0.1,
) -> bool:
    """Wait until the user stopped typing for `gap` s (max `max_wait` s). False on timeout."""
    deadline = monotonic() + max_wait
    while True:
        now = monotonic()
        if now - io.last_user_input_at >= gap:
            return True
        if now >= deadline:
            return False
        await sleep(min(step, max(0.0, deadline - now)))
