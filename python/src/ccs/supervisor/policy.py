"""The limit policy (ADR-0008), pure: `evaluate(profile, snapshot, state, sessions, now)`.

Steps (P06 design):
1. data gate: only `status == ok` data creates warns and holds
2. spill: with credits enabled and `spill` on, session/weekly holds are suppressed/released
3. windows: warn once per window instance, pause once per instance (hysteresis)
4. extra usage: warn at `warn` % of the monthly cap; with spill, hold at `pause` %
5. reset confirmation: force a poll at `resets_at + 15 s`, clear on an advanced window or
   `percent < warn`, re-poll every 30 s, time-based clear after 10 min
6. apply holds to sessions → pause / resume / update / mark-running actions
7. events (`limit.warn|pause|resume`) carry data only; `EventBus.emit` adds the text.

Every threshold comes from `profile.limits` — no literals.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ccs.config.models import Profile
from ccs.events import Event
from ccs.supervisor import ledger
from ccs.supervisor.model import (
    EXTRA_USAGE,
    MANUAL,
    MODEL_SCOPED,
    OVERRIDDEN,
    PAUSED,
    SESSION,
    WEEKLY,
    Hold,
    LedgerEntry,
    ProfileSupervisorState,
    SessionView,
    bounded,
)
from ccs.usage.model import STATUS_OK, UsageSnapshot, format_iso
from ccs.usage.normalize import window_key_time

CONFIRM_DELAY = timedelta(seconds=15)
CONFIRM_REPOLL = timedelta(seconds=30)
CONFIRM_GIVE_UP = timedelta(minutes=10)
RESET_TOLERANCE = timedelta(seconds=60)  # get_usage `resets_at` jitters by sub-seconds


# ---------------------------------------------------------------- actions


@dataclass(frozen=True)
class PauseSession:
    wrapper_id: str
    hold_ids: tuple[str, ...]
    resume_at: datetime | None
    instances: tuple[str, ...]


@dataclass(frozen=True)
class ResumeSession:
    wrapper_id: str
    prompt: str | None


@dataclass(frozen=True)
class UpdateHolds:
    """A paused session whose applicable holds changed (record only, no command)."""

    wrapper_id: str
    hold_ids: tuple[str, ...]
    resume_at: datetime | None


@dataclass(frozen=True)
class MarkRunning:
    """overridden → running once the overridden instances are gone (no injection)."""

    wrapper_id: str


@dataclass(frozen=True)
class ForcePoll:
    at: datetime


@dataclass(frozen=True)
class LogWarning:
    message: str


Action = PauseSession | ResumeSession | UpdateHolds | MarkRunning | ForcePoll | LogWarning


@dataclass(frozen=True)
class Decision:
    actions: tuple[Action, ...]
    state: ProfileSupervisorState
    events: tuple[Event, ...]


@dataclass(frozen=True)
class _Window:
    kind: str
    hold_id: str
    name: str | None
    percent: int
    resets_at: datetime | None
    observed_at: datetime | None


# ---------------------------------------------------------------- helpers


def instance_key(kind: str, resets_at: datetime, name: str | None = None) -> str:
    """Window-instance key: `session:<K>`, `weekly:<K>`, `model_scoped:<name>:<K>`."""
    k = window_key_time(resets_at)
    return f"{kind}:{name}:{k}" if kind == MODEL_SCOPED and name else f"{kind}:{k}"


def extra_instance(now: datetime, limit: float | None) -> str:
    """`extra_usage:<YYYY-MM>:<cap>` (a raised cap re-arms the instance)."""
    cap = "none" if limit is None else f"{limit:g}"
    return f"{EXTRA_USAGE}:{now.astimezone(UTC).strftime('%Y-%m')}:{cap}"


def model_matches(scope: str | None, model_id: str | None) -> bool:
    """Whether a session's model belongs to a model-scoped bucket (unknown model → no)."""
    if not scope or not model_id:
        return False
    return scope.lower() in model_id.lower()


def applies(hold: Hold, session: SessionView) -> bool:
    """Whether `hold` pauses `session` (ADR-0008 "Pause affects" column)."""
    if hold.kind == MODEL_SCOPED:
        return model_matches(hold.scope, session.model_id)
    if hold.kind == MANUAL and hold.scope is not None:
        return hold.scope == session.wrapper_id
    return True


def applicable_holds(holds: Sequence[Hold], session: SessionView) -> list[Hold]:
    return [h for h in holds if applies(h, session)]


def latest_resume(holds: Sequence[Hold]) -> datetime | None:
    """The latest `resets_at` of `holds`; `None` when any has no reset time (or none)."""
    if not holds or any(h.resets_at is None for h in holds):
        return None
    return max(h.resets_at for h in holds if h.resets_at is not None)


def profile_resume_at(holds: Sequence[Hold]) -> datetime | None:
    """Snapshot `resume_at`: the latest `resets_at` among holds that have one."""
    times = [h.resets_at for h in holds if h.resets_at is not None]
    return max(times) if times else None


def _hold_ids(holds: Sequence[Hold]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(h.id for h in holds))


def _windows(snapshot: UsageSnapshot) -> list[_Window]:
    out: list[_Window] = []
    if snapshot.session is not None:
        s = snapshot.session
        out.append(_Window(SESSION, SESSION, None, s.percent, s.resets_at, s.observed_at))
    if snapshot.weekly is not None:
        w = snapshot.weekly
        out.append(_Window(WEEKLY, WEEKLY, None, w.percent, w.resets_at, w.observed_at))
    for m in snapshot.model_scoped:
        out.append(
            _Window(
                MODEL_SCOPED,
                f"{MODEL_SCOPED}:{m.name}",
                m.name,
                m.percent,
                m.resets_at,
                m.observed_at,
            )
        )
    return out


def _thresholds(profile: Profile, kind: str) -> tuple[int, int]:
    lim = profile.limits
    if kind == SESSION:
        return lim.session.warn, lim.session.pause
    if kind == WEEKLY:
        return lim.weekly.warn, lim.weekly.pause
    if kind == MODEL_SCOPED:
        return lim.model_scoped.warn, lim.model_scoped.pause
    return lim.extra_usage.warn, lim.extra_usage.pause


def _window_for(hold: Hold, snapshot: UsageSnapshot) -> _Window | None:
    for w in _windows(snapshot):
        if w.hold_id == hold.id:
            return w
    return None


def reset_confirmed(hold: Hold, snapshot: UsageSnapshot, profile: Profile) -> bool:
    """Fresh data observed after the hold's reset shows a new window or `percent < warn`."""
    assert hold.resets_at is not None
    window = _window_for(hold, snapshot)
    if window is None:  # the window is gone (inactive) in data fetched after the reset
        return snapshot.fetched_at is not None and snapshot.fetched_at > hold.resets_at
    if window.observed_at is None or window.observed_at <= hold.resets_at:
        return False
    if window.resets_at is None or window.resets_at > hold.resets_at + RESET_TOLERANCE:
        return True
    warn, _ = _thresholds(profile, hold.kind)
    return window.percent < warn


def _event(pid: str, etype: str, key: str | None, data: dict[str, object]) -> Event:
    return Event(etype, pid, f"{pid}:{key}" if key else None, dict(data))


# ---------------------------------------------------------------- evaluate


def evaluate(
    profile: Profile,
    snapshot: UsageSnapshot | None,
    state: ProfileSupervisorState,
    sessions: Sequence[SessionView],
    now: datetime,
) -> Decision:
    """One policy pass for one profile (see the module docstring)."""
    pid = profile.id
    lim = profile.limits
    enabled = profile.supervisor.enabled
    fresh = snapshot is not None and snapshot.status == STATUS_OK
    extra = snapshot.extra_usage if snapshot is not None else None
    spill_active = bool(lim.extra_usage.spill and extra is not None and extra.enabled)

    holds: list[Hold] = list(state.holds)
    warned: list[str] = list(state.warned)
    paused_inst: list[str] = list(state.paused_instances)
    released: list[str] = list(state.released_instances)
    entries: list[LedgerEntry] = []
    actions: list[Action] = []
    events: list[Event] = []
    created: list[Hold] = []
    cleared: list[tuple[Hold, str]] = []  # (hold, reason)
    percents: dict[str, int] = {}

    # 0. supervision switched off: nothing may stay paused
    if not enabled and holds:
        for h in holds:
            cleared.append((h, "supervisor_disabled"))
            entries.append(
                ledger.entry(now, "release", instance=h.instance, detail="supervisor_disabled")
            )
        holds = []

    # 2. spill: work continues on credits → release session/weekly holds (re-armable)
    if spill_active:
        keep: list[Hold] = []
        for h in holds:
            if h.kind in (SESSION, WEEKLY):
                cleared.append((h, "spill_active"))
                entries.append(
                    ledger.entry(now, "release", instance=h.instance, detail="spill_active")
                )
                paused_inst = [i for i in paused_inst if i != h.instance]
            else:
                keep.append(h)
        holds = keep

    active_instances = {h.instance for h in holds}

    # 3. windows (fresh data only)
    if fresh and snapshot is not None:
        for w in _windows(snapshot):
            if w.resets_at is None or w.resets_at <= now:
                continue  # inactive / already ended window
            warn, pause = _thresholds(profile, w.kind)
            instance = instance_key(w.kind, w.resets_at, w.name)
            percents[instance] = w.percent
            base = {
                "window": w.kind,
                "name": w.name,
                "percent": w.percent,
                "resets_at": format_iso(w.resets_at),
            }
            if w.percent >= warn:
                key = f"warn:{instance}"
                if key not in warned:
                    warned.append(key)
                    events.append(_event(pid, "limit.warn", key, {**base, "level": "warn"}))
            if w.kind == MODEL_SCOPED and lim.model_scoped.warn_only:
                if w.percent >= pause:
                    key = f"warnp:{instance}"
                    if key not in warned:
                        warned.append(key)
                        events.append(
                            _event(pid, "limit.warn", key, {**base, "level": "pause_level"})
                        )
                continue
            if (
                w.percent >= pause
                and enabled
                and not (spill_active and w.kind in (SESSION, WEEKLY))
                and instance not in paused_inst
                and instance not in released
                and instance not in active_instances
            ):
                hold = Hold(
                    id=w.hold_id,
                    kind=w.kind,
                    instance=instance,
                    scope=w.name,
                    resets_at=w.resets_at,
                    created_at=now,
                )
                holds.append(hold)
                created.append(hold)
                paused_inst.append(instance)
                active_instances.add(instance)

    # 4. extra usage (credits)
    if fresh and extra is not None and extra.enabled and extra.percent is not None:
        instance = extra_instance(now, extra.limit)
        percents[instance] = extra.percent
        extra_base: dict[str, object] = {
            "window": EXTRA_USAGE,
            "name": None,
            "percent": extra.percent,
            "used": extra.used,
            "limit": extra.limit,
            "currency": extra.currency,
            "resets_at": None,
        }
        if extra.percent >= lim.extra_usage.warn:
            key = f"warn:{instance}"
            if key not in warned:
                warned.append(key)
                events.append(_event(pid, "limit.warn", key, {**extra_base, "level": "warn"}))
        if (
            spill_active
            and enabled
            and extra.percent >= lim.extra_usage.pause
            and instance not in paused_inst
            and instance not in released
            and not any(h.kind == EXTRA_USAGE for h in holds)
        ):
            hold = Hold(id=EXTRA_USAGE, kind=EXTRA_USAGE, instance=instance, created_at=now)
            holds.append(hold)
            created.append(hold)
            paused_inst.append(instance)

    # 5. reset confirmation / clearing
    remaining: list[Hold] = []
    for h in holds:
        if h in created or h.kind == MANUAL:
            remaining.append(h)
            continue
        if h.kind == EXTRA_USAGE:
            credits_ok = (
                extra is None
                or not extra.enabled
                or extra.percent is None
                or extra.percent < lim.extra_usage.pause
                or not lim.extra_usage.spill
            )
            if fresh and credits_ok:
                cleared.append((h, "credits"))
                entries.append(ledger.entry(now, "resume", instance=h.instance, detail="credits"))
            else:
                remaining.append(h)
            continue
        if h.resets_at is None or now < h.resets_at + CONFIRM_DELAY:
            remaining.append(h)
            continue
        if h.confirm_started_at is None:
            h = dataclasses.replace(h, confirm_started_at=now, next_confirm_poll_at=now)
        if fresh and snapshot is not None and reset_confirmed(h, snapshot, profile):
            cleared.append((h, "reset_confirmed"))
            entries.append(
                ledger.entry(now, "resume", instance=h.instance, detail="reset_confirmed")
            )
            continue
        assert h.confirm_started_at is not None
        if now >= h.confirm_started_at + CONFIRM_GIVE_UP:
            cleared.append((h, "time_based_clear"))
            released.append(h.instance)
            entries.append(ledger.entry(now, "time_based_clear", instance=h.instance))
            actions.append(
                LogWarning(
                    f"{pid}: could not confirm the reset of {h.instance} within 10 min; "
                    "resuming by time"
                )
            )
            continue
        if h.next_confirm_poll_at is None or now >= h.next_confirm_poll_at:
            actions.append(ForcePoll(now))
            h = dataclasses.replace(h, next_confirm_poll_at=now + CONFIRM_REPOLL)
        remaining.append(h)
    holds = remaining

    # 6. apply holds to sessions
    paused_by_instance: Counter[str] = Counter()
    resumed_wrappers: list[str] = []
    for s in sessions:
        sup = s.supervision
        applicable = applicable_holds(holds, s)
        overridden = set(sup.overridden_instances)
        # an override covers only the instances present when the user overrode (ADR-0007)
        effective = [h for h in applicable if h.instance not in overridden]
        ids = _hold_ids(effective)
        resume_at = latest_resume(effective)
        if sup.state == PAUSED:
            if not effective:
                prompt = profile.supervisor.resume_prompt if sup.was_busy_at_pause is True else None
                actions.append(ResumeSession(s.wrapper_id, prompt))
                resumed_wrappers.append(s.wrapper_id)
                entries.append(
                    ledger.entry(
                        now,
                        "resume",
                        wrapper_ids=[s.wrapper_id],
                        detail="prompt" if prompt else "no_prompt",
                    )
                )
            elif ids != sup.holds or resume_at != sup.resume_at:
                actions.append(UpdateHolds(s.wrapper_id, ids, resume_at))
            continue
        if effective:  # running or overridden with a new hold instance → pause
            actions.append(
                PauseSession(s.wrapper_id, ids, resume_at, tuple(h.instance for h in effective))
            )
            paused_by_instance.update(h.instance for h in effective)
            entries.append(
                ledger.entry(
                    now, "pause", instance=effective[0].instance, wrapper_ids=[s.wrapper_id]
                )
            )
        elif sup.state == OVERRIDDEN and not overridden & {h.instance for h in applicable}:
            actions.append(MarkRunning(s.wrapper_id))

    # 7. events
    for h in created:
        events.append(
            _event(
                pid,
                "limit.pause",
                f"pause:{h.instance}",
                {
                    "window": h.kind,
                    "name": h.scope if h.kind == MODEL_SCOPED else None,
                    "hold": h.id,
                    "percent": percents.get(h.instance),
                    "resume_at": format_iso(h.resets_at),
                    "sessions_paused": paused_by_instance.get(h.instance, 0),
                },
            )
        )
    for h, reason in cleared:
        events.append(
            _event(
                pid,
                "limit.resume",
                f"resume:{h.instance}",
                {
                    "window": h.kind,
                    "name": h.scope if h.kind == MODEL_SCOPED else None,
                    "hold": h.id,
                    "reason": reason,
                    "sessions_resumed": len(resumed_wrappers),
                },
            )
        )

    new_state = ProfileSupervisorState(
        holds=tuple(holds),
        warned=bounded(warned),
        paused_instances=bounded(paused_inst),
        released_instances=bounded(released),
        ledger=ledger.append(state.ledger, entries),
    )
    return Decision(tuple(actions), new_state, tuple(events))


# ---------------------------------------------------------------- manual control (pure)


def add_manual_hold(
    state: ProfileSupervisorState, now: datetime, wrapper_id: str | None = None
) -> tuple[ProfileSupervisorState, Hold, bool]:
    """A profile-wide (or session-scoped) `manual` hold; `(state, hold, created)`.

    Idempotent: an existing manual hold with the same scope is returned unchanged.
    """
    for h in state.holds:
        if h.kind == MANUAL and h.scope == wrapper_id:
            return state, h, False
    stamp = format_iso(now)
    instance = f"{MANUAL}:{wrapper_id}:{stamp}" if wrapper_id else f"{MANUAL}:{stamp}"
    hold = Hold(id=MANUAL, kind=MANUAL, instance=instance, scope=wrapper_id, created_at=now)
    entry = ledger.entry(
        now,
        "manual_pause",
        instance=instance,
        wrapper_ids=[wrapper_id] if wrapper_id else [],
    )
    new = dataclasses.replace(
        state,
        holds=(*state.holds, hold),
        paused_instances=bounded((*state.paused_instances, instance)),
        ledger=ledger.append(state.ledger, [entry]),
    )
    return new, hold, True


def release_holds(
    state: ProfileSupervisorState,
    now: datetime,
    *,
    keep: Sequence[Hold] = (),
    detail: str = "manual",
    wrapper_id: str | None = None,
) -> tuple[ProfileSupervisorState, list[Hold]]:
    """Clear every hold except `keep` (manual resume). Instances never re-pause."""
    keep_set = set(keep)
    cleared = [h for h in state.holds if h not in keep_set]
    if not cleared:
        return state, []
    entries = [
        ledger.entry(
            now,
            "manual_resume",
            instance=h.instance,
            wrapper_ids=[wrapper_id] if wrapper_id else [],
            detail=detail,
        )
        for h in cleared
    ]
    new = dataclasses.replace(
        state,
        holds=tuple(h for h in state.holds if h in keep_set),
        released_instances=bounded((*state.released_instances, *(h.instance for h in cleared))),
        ledger=ledger.append(state.ledger, entries),
    )
    return new, cleared


def supervisor_state_label(
    profile: Profile, snapshot: UsageSnapshot | None, holds: Sequence[Hold], now: datetime
) -> str:
    """`paused` (any hold), `warned` (a fresh window ≥ its warn threshold), else `normal`."""
    if holds:
        return "paused"
    if snapshot is None or snapshot.status != STATUS_OK:
        return "normal"
    for w in _windows(snapshot):
        if w.resets_at is not None and w.resets_at <= now:
            continue
        warn, _ = _thresholds(profile, w.kind)
        if w.percent >= warn:
            return "warned"
    extra = snapshot.extra_usage
    if (
        extra is not None
        and extra.enabled
        and extra.percent is not None
        and extra.percent >= profile.limits.extra_usage.warn
    ):
        return "warned"
    return "normal"
