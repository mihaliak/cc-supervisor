"""Warm-up skip rules (pure, ADR-0010).

`evaluate(ctx)` applies the rules in ADR order and returns the first skip reason, or
`Decision(run=True)`. `force` bypasses rules 3-7, never 1-2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from ccs.clock import local_tz
from ccs.config.models import Profile
from ccs.usage.model import STATUS_NEEDS_SIGN_IN, UsageSnapshot
from ccs.usage.normalize import session_window_active

MANUAL = "manual"
APP_START = "app_start"
UNLOCK_WAKE = "unlock_wake"
SCHEDULE = "schedule"
AUTO_CHAIN = "auto_chain"
TRIGGERS = (MANUAL, APP_START, UNLOCK_WAKE, SCHEDULE, AUTO_CHAIN)

# Skip reasons, in ADR-0010 rule order.
DISABLED = "disabled"
NEEDS_SIGN_IN = "needs_sign_in"
WINDOW_ACTIVE = "window_active"
SESSION_BUSY = "session_busy"
COOLDOWN = "cooldown"
OUTSIDE_ACTIVE_HOURS = "outside_active_hours"
WEEKLY_HOLD = "weekly_hold"
# Runtime reasons (not rules): another warm-up of the profile is still running.
IN_PROGRESS = "in_progress"

BUSY_ACTIVITIES = frozenset({"busy", "shell", "waiting"})


@dataclass(frozen=True)
class WarmupContext:
    """Everything the rules look at. `catch_up` marks a missed-schedule run (rule 6 applies)."""

    profile: Profile
    trigger: str
    now: datetime
    snapshot: UsageSnapshot | None
    sessions: Sequence[Mapping[str, Any]] = ()
    holds: Sequence[str] = ()
    last_attempt_at: datetime | None = None
    force: bool = False
    catch_up: bool = False
    tz: tzinfo | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Decision:
    run: bool
    reason: str | None = None


RUN = Decision(True, None)


def parse_hhmm(value: str) -> tuple[int, int]:
    """`"06:05"` → `(6, 5)`. Raises `ValueError` for anything else."""
    hh, sep, mm = value.partition(":")
    if not sep or len(hh) != 2 or len(mm) != 2 or not (hh + mm).isdigit():
        raise ValueError(f"not HH:MM: {value!r}")
    h, m = int(hh), int(mm)
    if h > 23 or m > 59:
        raise ValueError(f"not HH:MM: {value!r}")
    return h, m


def in_active_hours(now: datetime, start: str, end: str, tz: tzinfo) -> bool:
    """Whether local `now` falls in `[start, end)`; wraps past midnight when `start > end`.

    `start == end` means the whole day.
    """
    local = now.astimezone(tz)
    minute = local.hour * 60 + local.minute
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    s, e = sh * 60 + sm, eh * 60 + em
    if s == e:
        return True
    if s < e:
        return s <= minute < e
    return minute >= s or minute < e


def trigger_enabled(profile: Profile, trigger: str) -> bool:
    """Rule 1 toggle: `manual` has none; `schedule` means the schedule list is non-empty."""
    triggers = profile.warmup.triggers
    if trigger == MANUAL:
        return True
    if trigger == SCHEDULE:
        return bool(triggers.schedule)
    if trigger == APP_START:
        return triggers.app_start
    if trigger == UNLOCK_WAKE:
        return triggers.unlock_wake
    if trigger == AUTO_CHAIN:
        return triggers.auto_chain
    return False


def session_busy(sessions: Sequence[Mapping[str, Any]]) -> bool:
    """Rule 4: a supervised session is doing work (`idle`/`unknown` don't count)."""
    return any(str(rec.get("activity") or "unknown") in BUSY_ACTIVITIES for rec in sessions)


def evaluate(ctx: WarmupContext) -> Decision:
    """Apply the ADR-0010 skip rules in order; the first match wins."""
    profile = ctx.profile
    warmup = profile.warmup
    # 1. disabled
    if not warmup.enabled or not trigger_enabled(profile, ctx.trigger):
        return Decision(False, DISABLED)
    # 2. needs sign-in
    if ctx.snapshot is not None and ctx.snapshot.status == STATUS_NEEDS_SIGN_IN:
        return Decision(False, NEEDS_SIGN_IN)
    if ctx.force:
        return RUN
    # 3. session window already active
    if session_window_active(ctx.snapshot, ctx.now):
        return Decision(False, WINDOW_ACTIVE)
    # 4. a supervised session is busy (the window starts anyway)
    if session_busy(ctx.sessions):
        return Decision(False, SESSION_BUSY)
    # 5. cooldown (counts real attempts only)
    if ctx.last_attempt_at is not None and ctx.now - ctx.last_attempt_at < timedelta(
        minutes=warmup.cooldown_minutes
    ):
        return Decision(False, COOLDOWN)
    # 6. active hours: auto-chain and missed-schedule catch-up only
    if ctx.trigger == AUTO_CHAIN or (ctx.trigger == SCHEDULE and ctx.catch_up):
        hours = warmup.active_hours
        if not in_active_hours(ctx.now, hours.start, hours.end, ctx.tz or local_tz()):
            return Decision(False, OUTSIDE_ACTIVE_HOURS)
    # 7. weekly hold active
    if any(h == "weekly" for h in ctx.holds):
        return Decision(False, WEEKLY_HOLD)
    return RUN
