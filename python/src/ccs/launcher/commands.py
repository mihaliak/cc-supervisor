"""Launcher side of ADR-0007: execute the daemon's `pause` / `resume` commands.

The daemon decides *when* (P06); the launcher only injects ESC (pause) or the resume
prompt (resume) into the PTY and acks what happened. Input always passes through; a submit
while paused is reported once as `input_submitted_while_paused` (manual override).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ccs.launcher import inject
from ccs.launcher.session_map import SessionInfo, is_busy, is_known_busy

log = logging.getLogger(__name__)

Lookup = Callable[[], Awaitable[SessionInfo]]
Report = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class Timings:
    """Delays (seconds). Tests shrink them."""

    esc_verify_s: float = 2.0
    resume_poll_s: float = 2.0
    resume_max_s: float = 30.0
    submit_delay_s: float = 0.05
    typing_gap_s: float = 1.5
    typing_max_s: float = 30.0


async def _no_report(kind: str, detail: dict[str, Any]) -> None:
    return None


class CommandHandler:
    """Handles `{"cmd": …}` pushes; returns `(result, detail)` for the ack."""

    def __init__(
        self,
        io: inject.ChildIO,
        lookup: Lookup,
        report: Report | None = None,
        *,
        timings: Timings | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.io = io
        self.lookup = lookup
        self.report: Report = report or _no_report
        self.t = timings or Timings()
        self.monotonic = monotonic
        self.paused = False
        self.override_reported = False
        self._tasks: set[asyncio.Task[None]] = set()

    # ---------------------------------------------------------------- supervision state

    def apply_supervision(self, reply: dict[str, Any]) -> None:
        """Sync the local paused flag from a `register_wrapper` reply."""
        sup = reply.get("supervision")
        if not isinstance(sup, dict):
            return
        state = sup.get("state")
        if state == "paused":
            self.paused = True
        elif state in ("running", "overridden"):
            self.paused = False
            self.override_reported = state == "overridden"

    def on_user_submit(self) -> None:
        """Proxy callback: the user submitted input (Enter) — report an override once."""
        if not self.paused or self.override_reported:
            return
        self.override_reported = True
        self._spawn(self.report("input_submitted_while_paused", {}))

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            with contextlib.suppress(Exception):
                await coro

        task = asyncio.ensure_future(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- commands

    async def handle(self, cmd: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Run one command; never raises."""
        ctype = cmd.get("type")
        cmd_id = cmd.get("cmd_id")
        try:
            if ctype == "pause":
                return await self._pause(cmd_id)
            if ctype == "resume":
                prompt = cmd.get("prompt")
                return await self._resume(cmd_id, prompt if isinstance(prompt, str) else None)
        except Exception as exc:
            log.exception("command %s failed", ctype)
            with contextlib.suppress(Exception):
                await self.report("inject_failed", {"cmd_id": cmd_id, "error": str(exc)})
            return "failed", {"reason": str(exc)}
        return "failed", {"reason": "unknown_cmd"}

    async def _guard(self) -> None:
        await inject.wait_for_typing_gap(
            self.io,
            gap=self.t.typing_gap_s,
            max_wait=self.t.typing_max_s,
            monotonic=self.monotonic,
        )

    async def _pause(self, cmd_id: Any) -> tuple[str, dict[str, Any]]:
        self.paused = True
        self.override_reported = False
        info = await self.lookup()
        if not is_busy(info.status):
            return "skipped", {"was_busy": False, "session_id": info.session_id}
        await self._guard()
        self.io.write_to_child(inject.ESC)
        await self.report("injected", {"cmd_id": cmd_id, "what": "esc"})
        await asyncio.sleep(self.t.esc_verify_s)
        after = await self.lookup()
        if is_known_busy(after.status):
            # still working: one more ESC (a lone ESC can be read as an escape-sequence prefix)
            self.io.write_to_child(inject.ESC)
            await self.report("injected", {"cmd_id": cmd_id, "what": "esc"})
            await asyncio.sleep(self.t.esc_verify_s)
            after = await self.lookup()
        return "injected", {
            "was_busy": True,
            "interrupted": after.status == "idle",
            "status": after.status,
            "session_id": after.session_id or info.session_id,
        }

    async def _resume(self, cmd_id: Any, prompt: str | None) -> tuple[str, dict[str, Any]]:
        self.paused = False
        if not prompt:
            return "skipped", {"reason": "no_prompt"}
        deadline = self.monotonic() + self.t.resume_max_s
        info = await self.lookup()
        # `unknown` does not block the resume: a prompt typed into a busy TUI is queued.
        while is_known_busy(info.status):
            if self.monotonic() >= deadline:
                return "skipped", {"reason": "busy", "session_id": info.session_id}
            await asyncio.sleep(self.t.resume_poll_s)
            info = await self.lookup()
        await self._guard()
        self.io.write_to_child(inject.paste(prompt))
        await asyncio.sleep(self.t.submit_delay_s)
        self.io.write_to_child(inject.SUBMIT)
        await self.report("injected", {"cmd_id": cmd_id, "what": "resume_prompt"})
        return "injected", {"session_id": info.session_id}
