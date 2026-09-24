"""Warm-up scheduler (P08, ADR-0010): schedules, auto-chain, catch-up, `warmup` op.

Daemon extension (`install(daemon)`). Owns the rules evaluation for every trigger and
starts `WarmupRunner` runs in the background:

- **schedule**: each `{time, weekdays}` occurrence (local, DST-aware) runs once; a
  nonexistent local time (spring-forward gap) runs at the first valid minute after, an
  ambiguous one (fall-back fold) at its first occurrence.
- **clock jump / sleep**: a tick gap > 2x the tick is a wake → one catch-up per profile for
  the latest missed occurrence of the *same local day* (rule 6 applies to catch-ups).
- **auto_chain**: armed at `resets_at + 15 s` of the session window; forces polls until the
  reset is confirmed (retry every 30 s, give up after 10 min), then evaluates `auto_chain`.
- **op `warmup`**: evaluates synchronously, returns decisions, runs continue in the
  background; `unlock_wake` first runs the missed-schedule catch-up.

Reset confirmation for auto-chain is done here (P06 exposes no reset signal).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from ccs import claude_cli, fsio, paths
from ccs.clock import local_tz
from ccs.config.models import Config, Profile, ScheduleEntry
from ccs.usage.model import STATUS_OK, UsageSnapshot, format_iso
from ccs.warmup import rules
from ccs.warmup.runner import WarmupRunner
from ccs.warmup.state import WarmupStore

if TYPE_CHECKING:
    from ccs.daemon.server import Conn, Daemon

log = logging.getLogger(__name__)

TICK_S = 30.0
JUMP_FACTOR = 2  # a tick gap above 2x the tick counts as sleep / clock jump
STARTUP_LOOKBACK = timedelta(minutes=10)  # daemon (re)start: run occurrences due this recently
CHAIN_DELAY = timedelta(seconds=15)
CHAIN_RETRY = timedelta(seconds=30)
CHAIN_GIVE_UP = timedelta(minutes=10)
SAME_WINDOW = timedelta(seconds=60)  # resets_at jitter tolerance (P00-S2)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ---------------------------------------------------------------- occurrences (pure)


def resolve_local(day: date, hhmm: str, tz: tzinfo) -> datetime:
    """`day` at local `HH:MM` as UTC; gap → first valid minute after, fold → first."""
    h, m = rules.parse_hhmm(hhmm)
    naive = datetime.combine(day, time(h, m))
    for step in range(0, 181):  # DST gaps are ≤ 3 h anywhere
        candidate = naive + timedelta(minutes=step)
        local = candidate.replace(tzinfo=tz, fold=0)
        utc = local.astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == candidate:
            return utc
    return naive.replace(tzinfo=tz).astimezone(UTC)


def _entry_days(entry: ScheduleEntry) -> set[int]:
    return {WEEKDAYS.index(w) for w in entry.weekdays if w in WEEKDAYS}


def occurrences_between(
    entries: Iterable[ScheduleEntry], start: datetime, end: datetime, tz: tzinfo
) -> list[datetime]:
    """Occurrences `o` with `start < o <= end` (UTC, sorted, unique)."""
    if end <= start:
        return []
    first = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    found: set[datetime] = set()
    for entry in entries:
        days = _entry_days(entry)
        day = first
        while day <= last:
            if day.weekday() in days:
                try:
                    occ = resolve_local(day, entry.time, tz)
                except ValueError:
                    break
                if start < occ <= end:
                    found.add(occ)
            day += timedelta(days=1)
    return sorted(found)


def next_occurrence(
    entries: Iterable[ScheduleEntry], after: datetime, tz: tzinfo
) -> datetime | None:
    """The first occurrence strictly after `after` (within the next 8 days)."""
    found = occurrences_between(entries, after, after + timedelta(days=8), tz)
    return found[0] if found else None


def _hold_ids(value: Any) -> list[str]:
    """Hold ids from a P06 `holds` list (strings or `{id|kind}` objects)."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping) and item.get("active") is not False:
            ident = item.get("id") or item.get("kind")
            if isinstance(ident, str):
                out.append(ident)
    return out


# ---------------------------------------------------------------- scheduler


@dataclass
class ChainState:
    """Auto-chain for one session window instance (`resets_at`)."""

    resets_at: datetime
    fire_at: datetime
    give_up_at: datetime
    next_check_at: datetime
    polling: bool = False


class Scheduler:
    """All warm-up triggers of the daemon."""

    def __init__(
        self,
        daemon: Daemon,
        *,
        runner: WarmupRunner | None = None,
        store: WarmupStore | None = None,
        tick_s: float = TICK_S,
        tz: tzinfo | None = None,
    ) -> None:
        self.daemon = daemon
        self.store = store or WarmupStore()
        self.runner = runner or WarmupRunner(
            clock=daemon.clock,
            store=self.store,
            emit=daemon.emit,
            poll=lambda pid: daemon.request_poll(pid),
            resolve_claude=lambda: claude_cli.resolve_claude(daemon.config),
        )
        self.tick_s = tick_s
        self._tz = tz
        self.last_tick: datetime | None = None
        self.chains: dict[str, ChainState] = {}
        self._handled: dict[str, datetime] = {}  # last window reset confirmed or given up
        self._next: dict[str, datetime | None] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending: set[str] = set()  # reserved at spawn time, before the runner's lock

    @property
    def tz(self) -> tzinfo:
        return self._tz or local_tz()

    # -- helpers

    def _profiles(self) -> list[Profile]:
        cfg = self.daemon.config
        return list(cfg.profiles) if cfg is not None else []

    def holds_for(self, profile_id: str) -> list[str]:
        """Active hold ids: P06 contributions (`holds`) plus `supervisor/<id>.json`."""
        ids = _hold_ids(self.daemon.supervisor_state(profile_id).get("holds"))
        doc = fsio.read_json(paths.supervisor_file(profile_id))
        if doc is not None:
            ids += _hold_ids(doc.get("holds"))
        return ids

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        def _done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception() is not None:
                log.error("warm-up task failed", exc_info=t.exception())

        task.add_done_callback(_done)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- evaluation

    def context(
        self, profile: Profile, trigger: str, *, force: bool = False, catch_up: bool = False
    ) -> rules.WarmupContext:
        return rules.WarmupContext(
            profile=profile,
            trigger=trigger,
            now=self.daemon.clock.now(),
            snapshot=self.daemon.snapshots.get(profile.id),
            sessions=self.daemon.sessions_for(profile.id),
            holds=self.holds_for(profile.id),
            last_attempt_at=self.store.last_attempt_at(profile.id),
            force=force,
            catch_up=catch_up,
            tz=self.tz,
        )

    def evaluate_and_start(
        self, profile: Profile, trigger: str, *, force: bool = False, catch_up: bool = False
    ) -> rules.Decision:
        """Apply the rules; on `run`, start the warm-up in the background."""
        decision = rules.evaluate(self.context(profile, trigger, force=force, catch_up=catch_up))
        if decision.run and self.is_running(profile.id):
            decision = rules.Decision(False, rules.IN_PROGRESS)
        if not decision.run:
            assert decision.reason is not None
            log.info("warm-up %s (%s) skipped: %s", profile.id, trigger, decision.reason)
            self.runner.skip(profile.id, trigger, decision.reason)
            return decision
        log.info("warm-up %s (%s) starting", profile.id, trigger)
        self._pending.add(profile.id)
        self._spawn(self._run(profile, trigger))
        return decision

    def is_running(self, profile_id: str) -> bool:
        return profile_id in self._pending or self.runner.is_running(profile_id)

    async def _run(self, profile: Profile, trigger: str) -> None:
        try:
            await self.runner.run(profile, trigger)
        finally:
            self._pending.discard(profile.id)
        self._refresh_next(profile.id)

    # -- schedule

    def _due(self, profile: Profile, start: datetime, end: datetime) -> list[datetime]:
        entries = profile.warmup.triggers.schedule
        if not entries:
            return []
        consumed = self.store.consumed(profile.id)
        return [
            o
            for o in occurrences_between(entries, start, end, self.tz)
            if format_iso(o) not in consumed
        ]

    def run_due_schedules(self, start: datetime, end: datetime) -> None:
        """Regular tick: every profile's due occurrence runs once (latest wins)."""
        for profile in self._profiles():
            due = self._due(profile, start, end)
            if not due:
                continue
            self.store.mark_consumed(profile.id, due, end)
            self.evaluate_and_start(profile, rules.SCHEDULE)

    def catch_up(self, start: datetime, now: datetime) -> None:
        """Wake: at most one catch-up per profile, for the latest same-local-day occurrence."""
        today = now.astimezone(self.tz).date()
        for profile in self._profiles():
            due = self._due(profile, start, now)
            if not due:
                continue
            self.store.mark_consumed(profile.id, due, now)
            same_day = [o for o in due if o.astimezone(self.tz).date() == today]
            if same_day:
                log.info("warm-up %s: catch-up for missed %s", profile.id, format_iso(same_day[-1]))
                self.evaluate_and_start(profile, rules.SCHEDULE, catch_up=True)

    # -- auto-chain

    def arm(self, profile_id: str) -> None:
        """(Re-)arm the auto-chain timer from the current session `resets_at`."""
        profile = self.daemon.profile(profile_id)
        if profile is None or not (profile.warmup.enabled and profile.warmup.triggers.auto_chain):
            self.chains.pop(profile_id, None)
            return
        snap = self.daemon.snapshots.get(profile_id)
        reset = snap.session.resets_at if snap and snap.session else None
        if reset is None:
            return  # inactive now: keep a pending chain (it confirms the reset)
        now = self.daemon.clock.now()
        current = self.chains.get(profile_id)
        if current is not None and abs(reset - current.resets_at) <= SAME_WINDOW:
            return
        handled = self._handled.get(profile_id)
        if handled is not None and abs(reset - handled) <= SAME_WINDOW:
            return  # this window's reset was already confirmed (or given up on)
        # a reset that already passed (e.g. daemon restart) is confirmed right away
        fire = now if reset <= now and current is None else reset + CHAIN_DELAY
        self.chains[profile_id] = ChainState(
            resets_at=reset, fire_at=fire, give_up_at=fire + CHAIN_GIVE_UP, next_check_at=fire
        )

    @staticmethod
    def reset_confirmed(snap: UsageSnapshot | None, chain: ChainState, now: datetime) -> bool:
        """Fresh data shows the window reset (inactive) or moved on (a new `resets_at`)."""
        if snap is None or snap.status != STATUS_OK:
            return False
        reset = snap.session.resets_at if snap.session else None
        if reset is None or reset <= now:
            return reset is None or abs(reset - chain.resets_at) > SAME_WINDOW
        return abs(reset - chain.resets_at) > SAME_WINDOW

    def check_chains(self, now: datetime) -> None:
        for pid, chain in list(self.chains.items()):
            if chain.polling or now < chain.next_check_at:
                continue
            if now >= chain.give_up_at:
                log.warning("auto-chain %s: reset not confirmed after 10 min; giving up", pid)
                self.chains.pop(pid, None)
                self._handled[pid] = chain.resets_at
                continue
            chain.polling = True
            self._spawn(self._confirm(pid, chain))

    async def _confirm(self, profile_id: str, chain: ChainState) -> None:
        try:
            snap = await self.daemon.request_poll(profile_id)
        finally:
            chain.polling = False
        if self.chains.get(profile_id) is not chain:
            return  # re-armed meanwhile
        now = self.daemon.clock.now()
        if not self.reset_confirmed(snap, chain, now):
            chain.next_check_at = now + CHAIN_RETRY
            return
        self.chains.pop(profile_id, None)
        self._handled[profile_id] = chain.resets_at
        profile = self.daemon.profile(profile_id)
        if profile is not None:
            self.evaluate_and_start(profile, rules.AUTO_CHAIN)
        self.arm(profile_id)
        self._refresh_next(profile_id)

    # -- next run

    def next_warmup(self, profile_id: str) -> tuple[datetime | None, str | None]:
        """`(when, trigger)` of the next planned automatic warm-up."""
        profile = self.daemon.profile(profile_id)
        if profile is None or not profile.warmup.enabled:
            return None, None
        now = self.daemon.clock.now()
        candidates: list[tuple[datetime, str]] = []
        if profile.warmup.triggers.schedule:
            occ = next_occurrence(profile.warmup.triggers.schedule, now, self.tz)
            if occ is not None:
                candidates.append((occ, rules.SCHEDULE))
        chain = self.chains.get(profile_id)
        if chain is not None and chain.fire_at > now:
            hours = profile.warmup.active_hours
            if rules.in_active_hours(chain.fire_at, hours.start, hours.end, self.tz):
                candidates.append((chain.fire_at, rules.AUTO_CHAIN))
        if not candidates:
            return None, None
        return min(candidates)

    def _refresh_next(self, profile_id: str) -> None:
        when, trigger = self.next_warmup(profile_id)
        changed = self.store.set_next(profile_id, when, trigger)
        if changed or self._next.get(profile_id) != when:
            self._next[profile_id] = when
            self.daemon.request_snapshot_write()

    def supervisor_fields(self, profile_id: str) -> dict[str, Any]:
        """`contribute_supervisor` hook: the snapshot's `next_warmup_at`."""
        return {"next_warmup_at": self.next_warmup(profile_id)[0]}

    # -- daemon hooks

    async def on_config_changed(self, old: Config | None, new: Config) -> None:
        ids = {p.id for p in new.profiles}
        for pid in list(self.chains):
            if pid not in ids:
                self.chains.pop(pid, None)
                self._handled.pop(pid, None)
        for pid in list(self._next):
            if pid not in ids:
                self._next.pop(pid, None)
                self.store.forget(pid)
        for profile in new.profiles:
            self.arm(profile.id)
            self._refresh_next(profile.id)

    async def on_usage_updated(self, profile_id: str) -> None:
        self.arm(profile_id)
        self._refresh_next(profile_id)

    async def tick(self, now: datetime | None = None) -> None:
        """One scheduler step (the loop calls it every `tick_s`; tests call it directly)."""
        now = now or self.daemon.clock.now()
        last = self.last_tick
        if last is None:
            self.run_due_schedules(now - STARTUP_LOOKBACK, now)
        elif now - last > timedelta(seconds=self.tick_s * JUMP_FACTOR):
            log.info("clock jump / wake detected (%ss gap)", int((now - last).total_seconds()))
            self.catch_up(last, now)
        else:
            self.run_due_schedules(last, now)
        self.last_tick = now
        self.check_chains(now)
        for profile in self._profiles():
            self._refresh_next(profile.id)

    async def loop(self) -> None:
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self.tick_s)
        finally:
            await self.shutdown()

    # -- socket op

    async def op_warmup(self, conn: Conn | None, msg: Mapping[str, Any]) -> dict[str, Any]:
        """`{profile_id? | all: true, trigger, force?}` → `{results: [...]}` (runs continue)."""
        trigger = msg.get("trigger") or rules.MANUAL
        if trigger not in rules.TRIGGERS:
            return {"ok": False, "error": "bad_trigger", "detail": str(trigger)}
        force = bool(msg.get("force"))
        pid = msg.get("profile_id")
        targets: Sequence[Profile]
        if msg.get("all") is True:
            targets = self._profiles()
        elif isinstance(pid, str) and pid:
            profile = self.daemon.profile(pid)
            if profile is None:
                return {"ok": False, "error": "unknown_profile"}
            targets = [profile]
        else:
            return {"ok": False, "error": "bad_request", "detail": "profile_id or all"}
        now = self.daemon.clock.now()
        if trigger == rules.UNLOCK_WAKE and self.last_tick is not None and now > self.last_tick:
            self.catch_up(self.last_tick, now)
            self.last_tick = now
        results: list[dict[str, Any]] = []
        for profile in targets:
            decision = self.evaluate_and_start(profile, str(trigger), force=force)
            entry: dict[str, Any] = {
                "profile_id": profile.id,
                "decision": "started" if decision.run else "skipped",
                "reason": decision.reason,
            }
            snap = self.daemon.snapshots.get(profile.id)
            if snap is not None and snap.session is not None:
                entry["resets_at"] = format_iso(snap.session.resets_at)
            results.append(entry)
        return {"ok": True, "trigger": trigger, "results": results}


def install(daemon: Daemon, *, tick_s: float = TICK_S) -> Scheduler:
    """Daemon extension entry point (P04 `ccs.daemon.extensions`)."""
    sched = Scheduler(daemon, tick_s=tick_s)
    daemon.hooks.on_config_changed(sched.on_config_changed)
    daemon.hooks.on_usage_updated(sched.on_usage_updated)
    daemon.hooks.contribute_supervisor(sched.supervisor_fields)
    daemon.hooks.handle_op("warmup", sched.op_warmup)
    daemon.add_task(sched.loop)
    return sched
