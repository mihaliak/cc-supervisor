"""Launcher side of ADR-0007: execute the daemon's `pause` / `resume` commands.

The daemon decides *when* (P06); the launcher only injects ESC (pause) or the resume
prompt (resume) into the PTY and acks what happened. Input always passes through; a submit
while paused is reported once as `input_submitted_while_paused` (manual override), and resent
after a re-register until the daemon accepted it.

ADR-0022: a pause on `unknown` status still injects ESC but acks `was_busy: None` (no resume
prompt on a guess); no ESC once the user submitted during the pause; no resume prompt when the
user typed anything since the pause (it would merge with a draft).

Pauses carry a `pause_id`. The daemon resends a pause whose ack it never got when the launcher
re-registers: a pause already carried out answers with its first ack (no second ESC), and a
pause first learned from the `register_wrapper` reply keeps the typing and submits made since
its `paused_at`, so a re-register never forgets a draft or an override.
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
from ccs.usage.model import parse_time

log = logging.getLogger(__name__)

Lookup = Callable[[], Awaitable[SessionInfo]]
Ack = tuple[str, dict[str, Any]]
# `wrapper_event` sender; True once the daemon accepted the event
Report = Callable[[str, dict[str, Any]], Awaitable[bool | None]]


@dataclass(frozen=True)
class Timings:
    """Delays (seconds). Tests shrink them."""

    esc_verify_s: float = 2.0
    resume_poll_s: float = 2.0
    resume_max_s: float = 30.0
    submit_delay_s: float = 0.05
    typing_gap_s: float = 1.5
    typing_max_s: float = 30.0


async def _no_report(kind: str, detail: dict[str, Any]) -> bool:
    return False


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
        wallclock: Callable[[], float] = time.time,
    ) -> None:
        self.io = io
        self.lookup = lookup
        self.report: Report = report or _no_report
        self.t = timings or Timings()
        self.monotonic = monotonic
        self.wallclock = wallclock
        self.paused = False
        # per pause: the user submitted (override) / typed anything since the pause began
        self.submitted = False
        self.typed = False
        self.override_reported = False  # the daemon accepted the override report
        self._override_sending = False
        self._pause_no = 0
        self._pause_id: str | None = None  # the daemon's id of the current pause
        self._done: tuple[str, Ack] | None = None  # the last pause carried out, and its ack
        self._typed_at: float | None = None  # wall-clock time of the last typing / submit
        self._submitted_at: float | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ---------------------------------------------------------------- supervision state

    def apply_supervision(self, reply: dict[str, Any]) -> None:
        """Sync the local paused flag from a `register_wrapper` reply.

        Still paused after a re-register: an override the daemon did not get yet is resent.
        The same pause keeps its draft / override memory; a pause the launcher never got
        (its command was lost) starts with what the user did since its `paused_at`.
        """
        sup = reply.get("supervision")
        if not isinstance(sup, dict):
            return
        state = sup.get("state")
        if state == "paused":
            raw_id = sup.get("pause_id")
            pause_id = raw_id if isinstance(raw_id, str) and raw_id else None
            if not self._is_current(pause_id):
                self._missed_pause(pause_id, sup.get("paused_at"))
            self.paused = True
            self._send_override()
        elif state in ("running", "overridden"):
            self.paused = False
            self.override_reported = state == "overridden"

    def _is_current(self, pause_id: str | None) -> bool:
        """Whether `pause_id` names the pause this launcher is already in."""
        return self.paused and (pause_id is None or pause_id == self._pause_id)

    def _new_pause(self, pause_id: str | None = None) -> None:
        self._pause_no += 1
        self._pause_id = pause_id
        self.submitted = False
        self.typed = False
        self.override_reported = False

    def _missed_pause(self, pause_id: str | None, paused_at: Any) -> None:
        """A pause that began before this launcher heard of it (same wall clock as the daemon).

        Without a `paused_at` the draft memory is kept (never merge a prompt with a draft).
        """
        typed = self.typed
        self._new_pause(pause_id)
        since = parse_time(paused_at)
        if since is None:
            self.typed = typed
            return
        t = since.timestamp()
        self.typed = self._typed_at is not None and self._typed_at >= t
        self.submitted = self._submitted_at is not None and self._submitted_at >= t

    def on_user_input(self) -> None:
        """Proxy callback: the user typed something (not only terminal reports)."""
        self.typed = True
        self._typed_at = self.wallclock()

    def on_user_submit(self) -> None:
        """Proxy callback: the user submitted input (Enter) — report an override once."""
        self._submitted_at = self.wallclock()
        if not self.paused:
            return
        self.submitted = True
        self._send_override()

    def _send_override(self) -> None:
        """Report the override unless it is not due, already accepted or on its way."""
        if not self.submitted or self.override_reported or self._override_sending:
            return
        self._override_sending = True
        pause_no = self._pause_no

        async def send() -> None:
            try:
                accepted = await self.report("input_submitted_while_paused", {})
            finally:
                self._override_sending = False
            if accepted is True and pause_no == self._pause_no:
                self.override_reported = True

        self._spawn(send())

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
                raw_id = cmd.get("pause_id")
                return await self._pause(cmd_id, raw_id if isinstance(raw_id, str) else None)
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

    async def _pause(self, cmd_id: Any, pause_id: str | None = None) -> Ack:
        if pause_id and self._done is not None and self._done[0] == pause_id:
            return self._done[1]  # a resend: carried out already, only the ack got lost
        if not (pause_id and self._is_current(pause_id)):
            self._new_pause(pause_id)
        self.paused = True
        ack = await self._pause_steps(cmd_id)
        if pause_id:
            self._done = (pause_id, ack)
        return ack

    async def _pause_steps(self, cmd_id: Any) -> Ack:
        info = await self.lookup()
        if not is_busy(info.status):
            return "skipped", {"was_busy": False, "session_id": info.session_id}
        # `unknown` still gets an ESC, but only a known-busy session earns a resume prompt
        was_busy = True if is_known_busy(info.status) else None
        await self._guard()
        if self.submitted:
            return self._overridden(info.session_id)
        self.io.write_to_child(inject.ESC)
        await self.report("injected", {"cmd_id": cmd_id, "what": "esc"})
        await asyncio.sleep(self.t.esc_verify_s)
        after = await self.lookup()
        if is_known_busy(after.status):
            if self.submitted:  # the user's own turn is running now: leave it alone
                return self._overridden(after.session_id or info.session_id)
            # still working: one more ESC (a lone ESC can be read as an escape-sequence prefix)
            self.io.write_to_child(inject.ESC)
            await self.report("injected", {"cmd_id": cmd_id, "what": "esc"})
            await asyncio.sleep(self.t.esc_verify_s)
            after = await self.lookup()
        return "injected", {
            "was_busy": was_busy,
            "interrupted": after.status == "idle",
            "status": after.status,
            "session_id": after.session_id or info.session_id,
        }

    def _overridden(self, session_id: str | None) -> tuple[str, dict[str, Any]]:
        return "skipped", {"reason": "overridden", "session_id": session_id}

    async def _resume(self, cmd_id: Any, prompt: str | None) -> tuple[str, dict[str, Any]]:
        self.paused = False
        if not prompt:
            return "skipped", {"reason": "no_prompt"}
        if self.typed:
            return "skipped", {"reason": "user_input"}
        deadline = self.monotonic() + self.t.resume_max_s
        info = await self.lookup()
        # `unknown` does not block the resume: a prompt typed into a busy TUI is queued.
        while is_known_busy(info.status):
            if self.monotonic() >= deadline:
                return "skipped", {"reason": "busy", "session_id": info.session_id}
            await asyncio.sleep(self.t.resume_poll_s)
            info = await self.lookup()
        await self._guard()
        if self.typed:  # typed while we waited for idle: never merge with a draft
            return "skipped", {"reason": "user_input"}
        self.io.write_to_child(inject.paste(prompt))
        await asyncio.sleep(self.t.submit_delay_s)
        self.io.write_to_child(inject.SUBMIT)
        await self.report("injected", {"cmd_id": cmd_id, "what": "resume_prompt"})
        return "injected", {"session_id": info.session_id}
