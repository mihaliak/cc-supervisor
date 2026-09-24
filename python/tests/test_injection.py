"""Launcher command handler (ADR-0007): pause → ESC, resume → bracketed paste + Enter, overrides."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ccs.launcher import inject
from ccs.launcher.commands import CommandHandler, Timings
from ccs.launcher.session_map import SessionInfo

FAST = Timings(
    esc_verify_s=0.01,
    resume_poll_s=0.01,
    resume_max_s=0.2,
    submit_delay_s=0.0,
    typing_gap_s=0.0,
    typing_max_s=1.0,
)


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
        self.calls = 0

    async def __call__(self) -> SessionInfo:
        self.calls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SessionInfo("sid-1", status)


class Reports:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, detail: dict[str, Any]) -> None:
        self.items.append((kind, detail))


def run(handler: CommandHandler, cmd: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return asyncio.run(handler.handle(cmd))


def test_pause_busy_injects_esc() -> None:
    io, reports = IO(), Reports()
    h = CommandHandler(io, Script("busy", "idle"), reports, timings=FAST)
    result, detail = run(h, {"cmd_id": "c1", "type": "pause", "holds": ["session"]})
    assert result == "injected"
    assert detail == {
        "was_busy": True,
        "interrupted": True,
        "status": "idle",
        "session_id": "sid-1",
    }
    assert io.written == [inject.ESC]
    assert reports.items == [("injected", {"cmd_id": "c1", "what": "esc"})]
    assert h.paused is True


def test_pause_idle_skips() -> None:
    io = IO()
    h = CommandHandler(io, Script("idle"), timings=FAST)
    result, detail = run(h, {"cmd_id": "c1", "type": "pause"})
    assert result == "skipped" and detail == {"was_busy": False, "session_id": "sid-1"}
    assert io.written == []
    assert h.paused is True


def test_pause_unknown_counts_as_busy_single_esc() -> None:
    io = IO()
    h = CommandHandler(io, Script("unknown"), timings=FAST)
    result, detail = run(h, {"cmd_id": "c1", "type": "pause"})
    assert result == "injected" and detail["was_busy"] is True
    assert io.written == [inject.ESC]  # no retry on `unknown`
    assert detail["interrupted"] is False


def test_pause_still_busy_retries_once() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy", "busy", "idle"), timings=FAST)
    _result, detail = run(h, {"cmd_id": "c1", "type": "pause"})
    assert io.written == [inject.ESC, inject.ESC]
    assert detail["interrupted"] is True


def test_resume_with_prompt_pastes_and_submits() -> None:
    io, reports = IO(), Reports()
    h = CommandHandler(io, Script("idle"), reports, timings=FAST)
    h.paused = True
    result, _detail = run(h, {"cmd_id": "c2", "type": "resume", "prompt": "Continue."})
    assert result == "injected"
    assert io.written == [inject.paste("Continue."), inject.SUBMIT]
    assert reports.items == [("injected", {"cmd_id": "c2", "what": "resume_prompt"})]
    assert h.paused is False


def test_resume_without_prompt_skips() -> None:
    io = IO()
    h = CommandHandler(io, Script("idle"), timings=FAST)
    h.paused = True
    assert run(h, {"cmd_id": "c2", "type": "resume", "prompt": None}) == (
        "skipped",
        {"reason": "no_prompt"},
    )
    assert io.written == [] and h.paused is False


def test_resume_waits_for_idle_then_gives_up() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy"), timings=FAST)
    result, detail = run(h, {"cmd_id": "c", "type": "resume", "prompt": "go"})
    assert result == "skipped" and detail["reason"] == "busy"
    assert io.written == []


def test_resume_waits_while_busy_then_injects() -> None:
    io = IO()
    lookup = Script("busy", "busy", "idle")
    h = CommandHandler(io, lookup, timings=FAST)
    result, _ = run(h, {"cmd_id": "c", "type": "resume", "prompt": "go"})
    assert result == "injected" and lookup.calls == 3


def test_resume_unknown_does_not_block() -> None:
    io = IO()
    h = CommandHandler(io, Script("unknown"), timings=FAST)
    result, _ = run(h, {"cmd_id": "c", "type": "resume", "prompt": "go"})
    assert result == "injected"


def test_typing_guard_delays_resume() -> None:
    io = IO()
    timings = Timings(
        esc_verify_s=0.01,
        resume_poll_s=0.01,
        resume_max_s=1,
        submit_delay_s=0,
        typing_gap_s=0.3,
        typing_max_s=5,
    )
    h = CommandHandler(io, Script("idle"), timings=timings)
    io.last_user_input_at = time.monotonic()  # the user just typed
    started = time.monotonic()
    run(h, {"cmd_id": "c", "type": "resume", "prompt": "go"})
    assert time.monotonic() - started >= 0.29
    assert io.written[0] == inject.paste("go")


def test_unknown_cmd_fails() -> None:
    h = CommandHandler(IO(), Script("idle"), timings=FAST)
    assert run(h, {"cmd_id": "x", "type": "explode"}) == ("failed", {"reason": "unknown_cmd"})


def test_lookup_error_reports_failure() -> None:
    reports = Reports()

    async def broken() -> SessionInfo:
        raise RuntimeError("boom")

    h = CommandHandler(IO(), broken, reports, timings=FAST)
    result, detail = run(h, {"cmd_id": "x", "type": "pause"})
    assert result == "failed" and detail["reason"] == "boom"
    assert reports.items[0][0] == "inject_failed"


def test_override_reported_once() -> None:
    reports = Reports()

    async def body() -> None:
        h = CommandHandler(IO(), Script("idle"), reports, timings=FAST)
        h.on_user_submit()  # not paused: nothing
        h.paused = True
        h.on_user_submit()
        h.on_user_submit()
        await asyncio.sleep(0.05)

    asyncio.run(body())
    assert reports.items == [("input_submitted_while_paused", {})]


def test_apply_supervision() -> None:
    h = CommandHandler(IO(), Script("idle"), timings=FAST)
    h.apply_supervision({"supervision": {"state": "paused"}})
    assert h.paused is True
    h.apply_supervision({"supervision": {"state": "overridden"}})
    assert h.paused is False and h.override_reported is True
    h.apply_supervision({"supervision": None})
    assert h.paused is False
