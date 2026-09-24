"""Keystroke injection builders and the typing guard (ADR-0007).

Only ESC and the resume prompt text are ever injected. Never slash commands: P00-S3 showed
`/model <x>` persists `model` into the profile's settings.json.
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
