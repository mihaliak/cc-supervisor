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
    esc_gap_s=0.0,
    key_gap_s=0.0,
    sweep_settle_s=0.0,
)
STOP_AGENTS = list(inject.STOP_AGENTS)


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

    async def __call__(self, kind: str, detail: dict[str, Any]) -> bool:
        self.items.append((kind, detail))
        return True


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
    # ADR-0022: ESC on a guess, but `was_busy` stays unknown (no resume prompt on a guess)
    assert result == "injected" and detail["was_busy"] is None
    assert io.written == [inject.ESC]  # no retry on `unknown`
    assert detail["interrupted"] is False


def test_pause_still_busy_retries_esc_and_stops_background_agents() -> None:
    io, reports = IO(), Reports()
    h = CommandHandler(io, Script("busy", "busy", "idle"), reports, timings=FAST)
    _result, detail = run(h, {"cmd_id": "c1", "type": "pause"})
    # ADR-0023: busy after the ESC = background agents (ESC never stops them) or a lost ESC
    assert io.written == [inject.ESC, inject.ESC, *STOP_AGENTS]
    assert detail["interrupted"] is True and h.stopped_background is True
    assert [d["what"] for _, d in reports.items] == ["esc", "esc", "stop_agents"]


def test_pause_stops_workflows_row_by_row_until_idle() -> None:
    io, reports = IO(), Reports()
    # busy → ESC → busy → ESC + chord → busy (a workflow) → row 1 → busy → row 2 → idle
    lookup = Script("busy", "busy", "busy", "busy", "idle")
    h = CommandHandler(io, lookup, reports, timings=FAST)
    _result, detail = run(h, {"cmd_id": "c1", "type": "pause"})
    assert io.written == [
        inject.ESC,
        inject.ESC,
        *STOP_AGENTS,
        *inject.stop_row(1),
        *inject.stop_row(2),
    ]
    assert inject.stop_row(2) == (inject.DOWN, inject.DOWN, inject.STOP_ROW, inject.BACKSPACE)
    assert detail["interrupted"] is True
    whats = [d["what"] for _, d in reports.items]
    assert whats == ["esc", "esc", "stop_agents", "stop_workflow", "stop_workflow"]


def test_pause_sweep_is_bounded_then_esc_for_the_notice_turn() -> None:
    io = IO()
    timings = Timings(**{**FAST.__dict__, "sweep_rows": 2, "sweep_passes": 2})
    h = CommandHandler(io, Script("busy"), timings=timings)
    _result, detail = run(h, {"cmd_id": "c1", "type": "pause"})
    rows = [*inject.stop_row(1), *inject.stop_row(2)] * 2
    assert io.written == [inject.ESC, inject.ESC, *STOP_AGENTS, *rows, inject.ESC]
    assert detail["interrupted"] is False and detail["status"] == "busy"


def test_pause_never_sweeps_the_footer_over_a_draft() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy", "busy", "busy", "idle"), timings=FAST)
    h.on_user_input()  # typed after the last submit: the prompt may hold a draft
    run(h, {"cmd_id": "c1", "type": "pause"})
    assert io.written == [inject.ESC, inject.ESC, *STOP_AGENTS, inject.ESC]


def test_pause_sweep_stops_when_the_user_types() -> None:
    io = IO()
    h = CommandHandler(io, Script("idle"), timings=FAST)
    statuses = ["busy", "busy", "busy", "busy"]

    async def lookup() -> SessionInfo:
        if len(statuses) == 1:
            h.on_user_input()  # the user starts typing during the sweep
        return SessionInfo("sid-1", statuses.pop(0) if len(statuses) > 1 else statuses[0])

    h.lookup = lookup
    run(h, {"cmd_id": "c1", "type": "pause"})
    assert io.written == [inject.ESC, inject.ESC, *STOP_AGENTS, *inject.stop_row(1)]


def test_resume_after_stopping_background_work_adds_the_restart_note() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy", "busy", "idle"), timings=FAST)

    async def body() -> tuple[list[bytes], list[bytes]]:
        await h.handle({"cmd_id": "c1", "type": "pause", "pause_id": "p1"})
        await h.handle({"cmd_id": "r1", "type": "resume", "prompt": "Continue."})
        first, io.written = io.written, []
        h.lookup = Script("busy", "idle")  # a later pause stops no background work
        await h.handle({"cmd_id": "c2", "type": "pause", "pause_id": "p2"})
        await h.handle({"cmd_id": "r2", "type": "resume", "prompt": "Continue."})
        return first, io.written

    first, second = asyncio.run(body())
    assert inject.paste(f"Continue. {inject.RESTART_NOTE}") in first
    assert second == [inject.ESC, inject.paste("Continue."), inject.SUBMIT]


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


# ---------------------------------------------------------------- pause ids (resent pauses)


class Wall:
    """A settable wall clock (`time.time()` stand-in)."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def test_resent_pause_is_not_injected_twice() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy", "idle"), timings=FAST)

    async def body() -> tuple[tuple[str, dict[str, Any]], tuple[str, dict[str, Any]]]:
        first = await h.handle({"cmd_id": "c1", "type": "pause", "pause_id": "p1"})
        # the ack got lost with the connection; the daemon resends the same pause
        again = await h.handle({"cmd_id": "c2", "type": "pause", "pause_id": "p1"})
        return first, again

    first, again = asyncio.run(body())
    assert first == again and first[1]["was_busy"] is True
    assert io.written == [inject.ESC]  # one ESC, not two


def test_new_pause_id_is_a_new_pause() -> None:
    io = IO()
    h = CommandHandler(io, Script("busy", "idle", "busy", "idle"), timings=FAST)

    async def body() -> None:
        await h.handle({"cmd_id": "c1", "type": "pause", "pause_id": "p1"})
        await h.handle({"cmd_id": "c2", "type": "pause", "pause_id": "p2"})

    asyncio.run(body())
    assert io.written == [inject.ESC, inject.ESC]


def test_reregister_keeps_the_draft_of_a_pause_it_never_got() -> None:
    """The pause command was lost (link down); the user typed a draft after the pause began.
    Learning the pause from the register reply (then getting it resent) must not forget it."""
    io = IO()
    h = CommandHandler(io, Script("idle"), timings=FAST)
    paused_at = time.time() - 10
    h.on_user_input()  # a draft, typed while the daemon already had the session paused
    reply = {"supervision": {"state": "paused", "pause_id": "p1", "paused_at": iso(paused_at)}}

    async def body() -> tuple[str, dict[str, Any]]:
        h.apply_supervision(reply)
        await h.handle({"cmd_id": "c1", "type": "pause", "pause_id": "p1"})
        h.apply_supervision(reply)  # another re-register confirming the same pause
        return await h.handle({"cmd_id": "r", "type": "resume", "prompt": "Continue."})

    assert asyncio.run(body()) == ("skipped", {"reason": "user_input"})
    assert io.written == []


def test_typing_before_a_missed_pause_does_not_block_the_prompt() -> None:
    io, wall = IO(), Wall()
    h = CommandHandler(io, Script("idle"), timings=FAST, wallclock=wall)
    h.on_user_input()  # before the pause began
    wall.t += 60
    reply = {"supervision": {"state": "paused", "pause_id": "p1", "paused_at": iso(wall.t - 1)}}

    async def body() -> str:
        h.apply_supervision(reply)
        await h.handle({"cmd_id": "c1", "type": "pause", "pause_id": "p1"})
        return (await h.handle({"cmd_id": "r", "type": "resume", "prompt": "go"}))[0]

    assert asyncio.run(body()) == "injected"
    assert io.written == [inject.paste("go"), inject.SUBMIT]


def test_submit_during_a_missed_pause_is_an_override() -> None:
    io, wall, reports = IO(), Wall(), Reports()
    h = CommandHandler(io, Script("busy"), reports, timings=FAST, wallclock=wall)
    paused_at = wall.t
    wall.t += 2
    h.on_user_submit()  # the user started a turn after the pause began
    reply = {"supervision": {"state": "paused", "pause_id": "p1", "paused_at": iso(paused_at)}}

    async def body() -> tuple[str, dict[str, Any]]:
        h.apply_supervision(reply)
        result = await h.handle({"cmd_id": "c1", "type": "pause", "pause_id": "p1"})
        await asyncio.sleep(0.02)
        return result

    result, detail = asyncio.run(body())
    assert result == "skipped" and detail["reason"] == "overridden"
    assert io.written == []  # the user's own turn is not interrupted
    assert ("input_submitted_while_paused", {}) in reports.items
