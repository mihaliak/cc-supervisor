"""Warm-up scheduler: occurrences (weekdays, DST), ticks, catch-up, auto-chain, op (P08)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from warmup_helpers import (
    TZ,
    FakeDaemon,
    StubRunner,
    config,
    local,
    settle,
    snapshot,
    warmup,
)

from ccs.clock import FakeClock
from ccs.config.models import Config, Profile, ScheduleEntry
from ccs.daemon.scheduler import (
    CHAIN_DELAY,
    Scheduler,
    next_occurrence,
    occurrences_between,
    resolve_local,
)
from ccs.warmup import rules
from ccs.warmup.state import WarmupStore

ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def entries(time: str, days: list[str] | None = None) -> tuple[ScheduleEntry, ...]:
    return (ScheduleEntry(time, tuple(days or ALL_DAYS)),)


# ---------------------------------------------------------------- pure occurrence math


def test_resolve_local_plain() -> None:
    assert resolve_local(date(2026, 9, 24), "06:00", TZ) == local(2026, 9, 24, 6)


def test_resolve_local_spring_forward_gap_runs_at_first_valid_minute() -> None:
    # 2026-03-29: 02:00 CET → 03:00 CEST; 02:30 does not exist
    got = resolve_local(date(2026, 3, 29), "02:30", TZ)
    assert got == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 CEST
    assert got.astimezone(TZ).strftime("%H:%M") == "03:00"


def test_resolve_local_fall_back_fold_uses_first_occurrence() -> None:
    # 2026-10-25: 03:00 CEST → 02:00 CET; 02:30 happens twice
    got = resolve_local(date(2026, 10, 25), "02:30", TZ)
    assert got == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # the CEST one


def test_occurrences_weekday_boundary() -> None:
    mon6 = entries("06:00", ["mon"])
    sun_late = local(2026, 9, 27, 23, 59)  # Sunday
    assert next_occurrence(mon6, sun_late, TZ) == local(2026, 9, 28, 6)
    assert occurrences_between(mon6, sun_late, local(2026, 9, 28, 6), TZ) == [local(2026, 9, 28, 6)]
    # start exclusive, end inclusive
    assert occurrences_between(mon6, local(2026, 9, 28, 6), local(2026, 9, 28, 7), TZ) == []


def test_occurrences_weekdays_filter_and_multiple_entries() -> None:
    both = (ScheduleEntry("06:00", ("mon", "wed")), ScheduleEntry("12:30", ("wed",)))
    start = local(2026, 9, 27, 0)  # Sun
    got = occurrences_between(both, start, start + timedelta(days=7), TZ)
    assert got == [local(2026, 9, 28, 6), local(2026, 9, 30, 6), local(2026, 9, 30, 12, 30)]
    assert next_occurrence((ScheduleEntry("06:00", ()),), start, TZ) is None


# ---------------------------------------------------------------- scheduler harness


class SchedDaemon(FakeDaemon):
    """`FakeDaemon` plus `ensure_profile`: `reloaded` (a config set by the test) appears on
    the reload that an unknown profile triggers."""

    def __init__(self, cfg: Config, clock: FakeClock) -> None:
        super().__init__(cfg, clock)
        self.reloads = 0
        self.reloaded: Config | None = None

    async def ensure_profile(self, pid: str) -> Profile | None:
        if self.profile(pid) is None:
            self.reloads += 1
            if self.reloaded is not None:
                self.config = self.reloaded
        return self.profile(pid)


def harness(
    start: datetime, *profiles: dict[str, Any]
) -> tuple[Scheduler, SchedDaemon, StubRunner, FakeClock]:
    clock = FakeClock(start)
    daemon = SchedDaemon(config(*profiles), clock)
    runner = StubRunner()
    sched = Scheduler(daemon, runner=runner, store=WarmupStore(), tz=TZ)  # type: ignore[arg-type]
    return sched, daemon, runner, clock


def run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def sched_profile(time: str, days: list[str] | None = None, **over: Any) -> dict[str, Any]:
    trig = {"schedule": [{"time": time, "weekdays": days or ALL_DAYS}]}
    trig.update(over.pop("triggers", {}))
    return {"id": "work", "warmup": warmup(triggers=trig, **over)}


async def tick_until(sched: Scheduler, clock: FakeClock, end: datetime, step: float = 30) -> None:
    while clock.now() < end:
        clock.advance(step)
        await sched.tick()
        await settle()


def test_schedule_runs_once_and_survives_restart() -> None:
    async def body() -> None:
        sched, daemon, runner, clock = harness(local(2026, 9, 24, 5, 59), sched_profile("06:00"))
        await sched.tick()
        await tick_until(sched, clock, local(2026, 9, 24, 6, 5))
        assert runner.runs == [("work", rules.SCHEDULE)]
        # a daemon restart shortly after: the consumed occurrence is persisted
        again = Scheduler(daemon, runner=runner, store=WarmupStore(), tz=TZ)  # type: ignore[arg-type]
        await again.tick()
        await settle()
        assert runner.runs == [("work", rules.SCHEDULE)]

    run(body)


def test_restart_runs_recently_missed_occurrence() -> None:
    async def body() -> None:
        sched, _, runner, _ = harness(local(2026, 9, 24, 6, 4), sched_profile("06:00"))
        await sched.tick()  # first tick looks back 10 minutes
        await settle()
        assert runner.runs == [("work", rules.SCHEDULE)]

    run(body)


def test_dst_spring_forward_runs_at_0300() -> None:
    async def body() -> None:
        start = local(2026, 3, 29, 1, 50)
        sched, _, runner, clock = harness(start, sched_profile("02:30", ["sun"]))
        await sched.tick()
        fired_at: list[datetime] = []
        while clock.now() < start + timedelta(hours=2):
            clock.advance(30)
            before = len(runner.runs)
            await sched.tick()
            await settle()
            if len(runner.runs) > before:
                fired_at.append(clock.now())
        assert len(fired_at) == 1
        assert fired_at[0].astimezone(TZ).strftime("%H:%M") == "03:00"

    run(body)


def test_dst_fall_back_runs_once() -> None:
    async def body() -> None:
        start = local(2026, 10, 25, 1, 50)
        sched, _, runner, clock = harness(start, sched_profile("02:30", ["sun"]))
        await sched.tick()
        await tick_until(sched, clock, start + timedelta(hours=3))
        assert runner.runs == [("work", rules.SCHEDULE)]

    run(body)


def test_sleep_gap_catches_up_once_inside_active_hours() -> None:
    async def body() -> None:
        sched, _, runner, clock = harness(local(2026, 9, 24, 5, 50), sched_profile("06:00"))
        await sched.tick()
        clock.set(local(2026, 9, 24, 9, 10))
        await sched.tick()
        await settle()
        assert runner.runs == [("work", rules.SCHEDULE)]
        await tick_until(sched, clock, local(2026, 9, 24, 9, 20))
        assert len(runner.runs) == 1

    run(body)


def test_sleep_gap_catch_up_respects_active_hours() -> None:
    async def body() -> None:
        sched, _, runner, clock = harness(local(2026, 9, 24, 5, 50), sched_profile("06:00"))
        await sched.tick()
        clock.set(local(2026, 9, 24, 6, 40))  # woke before active hours (07:00)
        await sched.tick()
        await settle()
        assert runner.runs == []
        assert runner.skips == [("work", rules.SCHEDULE, rules.OUTSIDE_ACTIVE_HOURS)]

    run(body)


def test_sleep_across_midnight_no_catch_up_for_previous_day() -> None:
    async def body() -> None:
        sched, _, runner, clock = harness(local(2026, 9, 24, 23, 20), sched_profile("23:30"))
        await sched.tick()
        clock.set(local(2026, 9, 25, 0, 10))
        await sched.tick()
        await settle()
        assert runner.runs == [] and runner.skips == []

    run(body)


# ---------------------------------------------------------------- auto-chain


def chain_harness(
    start: datetime, **warm: Any
) -> tuple[Scheduler, SchedDaemon, StubRunner, FakeClock, datetime]:
    sched, daemon, runner, clock = harness(start, {"id": "work", "warmup": warmup(**warm)})
    reset = start + timedelta(hours=1)
    daemon.snapshots["work"] = snapshot(resets_at=reset, observed=start)
    return sched, daemon, runner, clock, reset


def test_auto_chain_fires_after_confirmed_reset() -> None:
    async def body() -> None:
        sched, daemon, runner, clock, reset = chain_harness(local(2026, 9, 24, 12))
        await sched.on_usage_updated("work")
        assert sched.chains["work"].fire_at == reset + CHAIN_DELAY
        clock.set(reset + timedelta(seconds=10))
        await sched.tick()
        await settle()
        assert daemon.polls == []
        daemon.poll_results["work"] = [snapshot(session=False, observed=reset)]
        clock.set(reset + timedelta(seconds=15))
        await sched.tick()
        await settle()
        assert daemon.polls == ["work"]
        assert runner.runs == [("work", rules.AUTO_CHAIN)]
        assert "work" not in sched.chains

    run(body)


def test_auto_chain_retries_until_confirmed() -> None:
    async def body() -> None:
        sched, daemon, runner, clock, reset = chain_harness(local(2026, 9, 24, 12))
        await sched.on_usage_updated("work")
        stale = snapshot(resets_at=reset, observed=reset)  # still the old window
        daemon.poll_results["work"] = [stale, snapshot(status="source_error"), None]
        daemon.poll_results["work"].append(snapshot(session=False))
        clock.set(reset + CHAIN_DELAY)
        for _ in range(4):
            await sched.tick()
            await settle()
            clock.advance(10)
            await sched.tick()  # not yet due (30 s retry)
            await settle()
            clock.advance(20)
        assert daemon.polls == ["work"] * 4
        assert runner.runs == [("work", rules.AUTO_CHAIN)]

    run(body)


def test_auto_chain_gives_up_after_10_minutes() -> None:
    async def body() -> None:
        sched, daemon, runner, clock, reset = chain_harness(local(2026, 9, 24, 12))
        await sched.on_usage_updated("work")
        daemon.snapshots["work"] = snapshot(resets_at=reset, observed=reset)  # never changes
        clock.set(reset + CHAIN_DELAY)
        await tick_until(sched, clock, reset + timedelta(minutes=12))
        assert runner.runs == []
        assert "work" not in sched.chains
        assert 15 <= len(daemon.polls) <= 21
        await sched.on_usage_updated("work")  # same stale window: not re-armed
        assert "work" not in sched.chains

    run(body)


def test_auto_chain_outside_active_hours_is_skipped() -> None:
    async def body() -> None:
        sched, daemon, runner, clock, reset = chain_harness(local(2026, 9, 24, 22, 30))
        await sched.on_usage_updated("work")  # resets 23:30, after active hours end
        assert sched.next_warmup("work") == (None, None)
        daemon.poll_results["work"] = [snapshot(session=False)]
        clock.set(reset + CHAIN_DELAY)
        await sched.tick()
        await settle()
        assert runner.runs == []
        assert runner.skips == [("work", rules.AUTO_CHAIN, rules.OUTSIDE_ACTIVE_HOURS)]

    run(body)


def test_auto_chain_rearms_on_new_window_and_ignores_jitter() -> None:
    async def body() -> None:
        sched, daemon, _, _, reset = chain_harness(local(2026, 9, 24, 12))
        await sched.on_usage_updated("work")
        daemon.snapshots["work"] = snapshot(resets_at=reset + timedelta(seconds=0.7))
        await sched.on_usage_updated("work")
        assert sched.chains["work"].resets_at == reset
        later = reset + timedelta(hours=2)
        daemon.snapshots["work"] = snapshot(resets_at=later)
        await sched.on_usage_updated("work")
        assert sched.chains["work"].resets_at == later

    run(body)


def test_auto_chain_disabled_disarms() -> None:
    async def body() -> None:
        sched, _, _, _, _ = chain_harness(local(2026, 9, 24, 12), triggers={"auto_chain": False})
        await sched.on_usage_updated("work")
        assert sched.chains == {}

    run(body)


def test_past_reset_after_restart_confirms_immediately() -> None:
    async def body() -> None:
        start = local(2026, 9, 24, 12)
        sched, daemon, runner, _ = harness(start, {"id": "work"})
        daemon.snapshots["work"] = snapshot(resets_at=start - timedelta(minutes=3))
        await sched.on_usage_updated("work")
        assert sched.chains["work"].fire_at == start
        daemon.poll_results["work"] = [snapshot(session=False)]
        await sched.tick()
        await settle()
        assert runner.runs == [("work", rules.AUTO_CHAIN)]

    run(body)


# ---------------------------------------------------------------- next_warmup_at


def test_next_warmup_is_min_of_schedule_and_chain() -> None:
    async def body() -> None:
        start = local(2026, 9, 24, 12)
        sched, daemon, _, _ = harness(start, sched_profile("06:00"))
        assert sched.next_warmup("work") == (local(2026, 9, 25, 6), rules.SCHEDULE)
        daemon.snapshots["work"] = snapshot(resets_at=start + timedelta(hours=2))
        await sched.on_usage_updated("work")
        when, trigger = sched.next_warmup("work")
        assert (when, trigger) == (start + timedelta(hours=2) + CHAIN_DELAY, rules.AUTO_CHAIN)
        assert sched.supervisor_fields("work") == {"next_warmup_at": when}
        doc = sched.store.view("work")
        assert doc["next_trigger"] == rules.AUTO_CHAIN and doc["next_scheduled_at"]
        assert daemon.snapshot_writes >= 1

    run(body)


def test_next_warmup_none_when_disabled() -> None:
    sched, _, _, _ = harness(local(2026, 9, 24, 12), sched_profile("06:00", enabled=False))
    assert sched.next_warmup("work") == (None, None)


# ---------------------------------------------------------------- op + evaluation


def two_profiles(start: datetime) -> tuple[Scheduler, SchedDaemon, StubRunner, FakeClock]:
    return harness(
        start,
        {"id": "work"},
        {
            "id": "personal",
            "warmup": warmup(triggers={"schedule": [{"time": "06:00", "weekdays": ALL_DAYS}]}),
        },
    )


def test_op_all_mixed_decisions() -> None:
    async def body() -> None:
        start = local(2026, 9, 24, 12)
        sched, daemon, runner, _ = two_profiles(start)
        daemon.snapshots["work"] = snapshot(resets_at=start + timedelta(hours=1))
        reply = await sched.op_warmup(None, {"all": True, "trigger": "app_start"})
        await settle()
        assert reply["ok"] and reply["trigger"] == "app_start"
        by_id = {r["profile_id"]: r for r in reply["results"]}
        assert by_id["work"]["decision"] == "skipped"
        assert by_id["work"]["reason"] == rules.WINDOW_ACTIVE
        assert by_id["work"]["resets_at"]
        assert by_id["personal"] == {
            "profile_id": "personal",
            "decision": "started",
            "reason": None,
        }
        assert runner.runs == [("personal", "app_start")]

    run(body)


def test_op_validation() -> None:
    async def body() -> None:
        sched, _, _, _ = two_profiles(local(2026, 9, 24, 12))
        assert (await sched.op_warmup(None, {"profile_id": "nope"}))["error"] == "unknown_profile"
        assert (await sched.op_warmup(None, {}))["error"] == "bad_request"
        bad = await sched.op_warmup(None, {"all": True, "trigger": "sometimes"})
        assert bad["error"] == "bad_trigger"
        ok = await sched.op_warmup(None, {"profile_id": "work"})
        assert ok["trigger"] == rules.MANUAL

    run(body)


def test_op_force_bypasses_window_rule() -> None:
    async def body() -> None:
        start = local(2026, 9, 24, 12)
        sched, daemon, runner, _ = two_profiles(start)
        daemon.snapshots["work"] = snapshot(resets_at=start + timedelta(hours=1))
        reply = await sched.op_warmup(None, {"profile_id": "work", "force": True})
        await settle()
        assert reply["results"][0]["decision"] == "started"
        assert runner.runs == [("work", rules.MANUAL)]

    run(body)


def test_op_unlock_wake_runs_catch_up_first_then_in_progress() -> None:
    async def body() -> None:
        sched, _, runner, clock = two_profiles(local(2026, 9, 24, 5, 50))
        await sched.tick()
        clock.set(local(2026, 9, 24, 9, 10))
        reply = await sched.op_warmup(None, {"all": True, "trigger": "unlock_wake"})
        by_id = {r["profile_id"]: r for r in reply["results"]}
        # personal: the missed 06:00 catch-up started, so unlock_wake sees it in progress
        assert by_id["personal"]["decision"] == "skipped"
        assert by_id["personal"]["reason"] == rules.IN_PROGRESS
        assert by_id["work"]["decision"] == "started"
        await settle()
        assert sorted(runner.runs) == [("personal", "schedule"), ("work", "unlock_wake")]
        assert sched.last_tick == clock.now()

    run(body)


def test_holds_from_contributions_and_supervisor_file() -> None:
    import json

    from ccs import paths

    sched, daemon, _, _ = harness(local(2026, 9, 24, 12), {"id": "work"})
    daemon.supervisor["work"] = {"holds": ["session", {"id": "manual"}]}
    paths.supervisor_file("work").parent.mkdir(parents=True, exist_ok=True)
    paths.supervisor_file("work").write_text(
        json.dumps({"holds": [{"id": "weekly", "kind": "weekly"}, {"id": "x", "active": False}]})
    )
    assert sched.holds_for("work") == ["session", "manual", "weekly"]
    decision = rules.evaluate(sched.context(daemon.profile("work"), rules.MANUAL))  # type: ignore[arg-type]
    assert decision.reason == rules.WEEKLY_HOLD


def test_busy_session_skips() -> None:
    async def body() -> None:
        sched, daemon, runner, _ = harness(local(2026, 9, 24, 12), {"id": "work"})
        daemon.sessions["work"] = [{"activity": "busy"}]
        reply = await sched.op_warmup(None, {"profile_id": "work", "trigger": "app_start"})
        assert reply["results"][0]["reason"] == rules.SESSION_BUSY
        assert runner.skips == [("work", "app_start", rules.SESSION_BUSY)]

    run(body)


def test_config_change_drops_removed_profiles() -> None:
    async def body() -> None:
        start = local(2026, 9, 24, 12)
        sched, daemon, _, _ = two_profiles(start)
        daemon.snapshots["work"] = snapshot(resets_at=start + timedelta(hours=1))
        await sched.on_usage_updated("work")
        assert "work" in sched.chains
        new = config({"id": "personal"})
        daemon.config = new
        await sched.on_config_changed(None, new)
        assert "work" not in sched.chains

    run(body)


def test_op_reloads_config_for_unknown_profile() -> None:
    async def body() -> None:
        sched, daemon, runner, _ = harness(local(2026, 9, 24, 12), {"id": "work"})
        daemon.reloaded = config({"id": "work"}, {"id": "new"})  # added since the last reload
        reply = await sched.op_warmup(None, {"profile_id": "new", "trigger": "app_start"})
        await settle()
        assert reply["ok"] and reply["results"][0]["profile_id"] == "new"
        assert daemon.reloads == 1 and runner.runs == [("new", "app_start")]
        missing = await sched.op_warmup(None, {"profile_id": "nope"})
        assert missing["error"] == "unknown_profile" and daemon.reloads == 2

    run(body)


def test_tick_survives_one_bad_profile() -> None:
    """A profile that breaks evaluation (e.g. `active_hours.end` "23:00\\n" before the validator
    rejected it) must not stop schedules, auto-chains or the loop for the others."""

    async def body() -> None:
        with pytest.raises(ValueError):
            rules.parse_hhmm("23:00\n")
        start = local(2026, 9, 24, 5, 59)
        bad_hours = {"start": "07:00", "end": "23:00\n"}
        bad = warmup(
            active_hours=bad_hours,
            triggers={"schedule": [{"time": "06:00", "weekdays": ALL_DAYS}]},
        )
        sched, daemon, runner, clock = harness(
            start, {"id": "bad", "warmup": bad}, sched_profile("06:00")
        )
        daemon.snapshots["bad"] = snapshot(resets_at=start + timedelta(hours=1))
        sched.arm("bad")
        assert "bad" in sched.chains
        await sched.tick()
        await tick_until(sched, clock, local(2026, 9, 24, 6, 1))
        assert ("work", rules.SCHEDULE) in runner.runs
        assert sched.next_warmup("work")[0] is not None
        # the loop itself keeps going (it used to die on the first bad tick)
        sched.tick_s = 0.01
        task = asyncio.ensure_future(sched.loop())
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    run(body)
