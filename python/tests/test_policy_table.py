"""The ADR-0008 policy, table-driven (P06): pure `evaluate` with synthetic snapshots."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ccs.config.models import Profile
from ccs.supervisor import policy
from ccs.supervisor.model import (
    Hold,
    ProfileSupervisorState,
    SessionView,
    Supervision,
)
from ccs.supervisor.policy import (
    Decision,
    ForcePoll,
    LogWarning,
    MarkRunning,
    PauseSession,
    ResumeSession,
    UpdateHolds,
    evaluate,
)
from ccs.usage.model import ExtraUsage, ScopedWindow, UsageSnapshot, Window

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
RESET = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
WEEK = datetime(2026, 9, 26, 6, 0, tzinfo=UTC)
PROMPT = "The usage limit window has reset. Continue exactly where you left off."


def prof(limits: dict[str, Any] | None = None, **extra: Any) -> Profile:
    raw: dict[str, Any] = {
        "id": "work",
        "flag": "work",
        "name": "Work",
        "emoji": "💼",
        "config_dir": "/tmp/cfg-work",
    }
    if limits:
        raw["limits"] = limits
    raw.update(extra)
    return Profile.from_dict(raw)


def snap(
    *,
    session: tuple[int, datetime | None] | None = None,
    weekly: tuple[int, datetime | None] | None = None,
    scoped: tuple[tuple[str, int, datetime | None], ...] = (),
    extra: ExtraUsage | None = None,
    status: str = "ok",
    at: datetime = NOW,
) -> UsageSnapshot:
    return UsageSnapshot(
        profile_id="work",
        status=status,
        fetched_at=at,
        polled_at=at,
        session=Window(session[0], session[1], at) if session else None,
        weekly=Window(weekly[0], weekly[1], at) if weekly else None,
        model_scoped=tuple(ScopedWindow(n, p, r, at) for n, p, r in scoped),
        extra_usage=extra,
    )


def credits(percent: int, *, enabled: bool = True, limit: float = 10.0) -> ExtraUsage:
    return ExtraUsage(
        enabled=enabled,
        percent=percent,
        used=limit * percent / 100,
        limit=limit,
        currency="EUR",
        disabled_reason=None if enabled else "out_of_credits",
    )


def sess(
    wid: str = "w1",
    *,
    model: str | None = "claude-opus-5-5",
    state: str = "running",
    holds: tuple[str, ...] = (),
    busy: bool | None = False,
    overridden: tuple[str, ...] = (),
    resume_at: datetime | None = None,
) -> SessionView:
    return SessionView(
        wrapper_id=wid,
        model_id=model,
        activity="busy",
        supervision=Supervision(
            state=state,
            holds=holds,
            resume_at=resume_at,
            was_busy_at_pause=busy,
            overridden_instances=overridden,
        ),
    )


def run(
    profile: Profile,
    snapshot: UsageSnapshot | None,
    sessions: tuple[SessionView, ...] = (sess(),),
    state: ProfileSupervisorState | None = None,
    now: datetime = NOW,
) -> Decision:
    return evaluate(profile, snapshot, state or ProfileSupervisorState(), sessions, now)


def events(d: Decision, etype: str) -> list[dict[str, Any]]:
    return [dict(e.data, _key=e.key) for e in d.events if e.type == etype]


def pauses(d: Decision) -> list[PauseSession]:
    return [a for a in d.actions if isinstance(a, PauseSession)]


def resumes(d: Decision) -> list[ResumeSession]:
    return [a for a in d.actions if isinstance(a, ResumeSession)]


def session_hold(resets: datetime = RESET, **kw: Any) -> Hold:
    return Hold(
        id="session",
        kind="session",
        instance=policy.instance_key("session", resets),
        resets_at=resets,
        created_at=NOW,
        **kw,
    )


# ---------------------------------------------------------------- thresholds


@pytest.mark.parametrize(
    ("window", "percent", "warns", "paused"),
    [
        ("session", 79, 0, False),
        ("session", 80, 1, False),
        ("session", 89, 1, False),
        ("session", 90, 1, True),
        ("weekly", 79, 0, False),
        ("weekly", 80, 1, False),
        ("weekly", 94, 1, False),
        ("weekly", 95, 1, True),
    ],
)
def test_session_and_weekly_thresholds(window: str, percent: int, warns: int, paused: bool) -> None:
    s = snap(session=(percent, RESET)) if window == "session" else snap(weekly=(percent, WEEK))
    d = run(prof(), s)
    assert len(events(d, "limit.warn")) == warns
    assert bool(pauses(d)) is paused
    assert [h.id for h in d.state.holds] == ([window] if paused else [])
    if paused:
        (p,) = pauses(d)
        assert p.wrapper_id == "w1" and p.hold_ids == (window,)
        assert p.resume_at == (RESET if window == "session" else WEEK)
        (pe,) = events(d, "limit.pause")
        assert pe["sessions_paused"] == 1 and pe["percent"] == percent
        assert pe["_key"] == f"work:pause:{d.state.holds[0].instance}"


def test_thresholds_come_from_the_profile() -> None:
    p = prof({"session": {"warn": 60, "pause": 70}})
    d = run(p, snap(session=(70, RESET)))
    assert pauses(d) and events(d, "limit.warn")
    assert not pauses(run(prof(), snap(session=(70, RESET))))


def test_warn_fires_once_per_instance() -> None:
    d1 = run(prof(), snap(session=(85, RESET)))
    d2 = run(prof(), snap(session=(87, RESET)), state=d1.state)
    assert len(events(d1, "limit.warn")) == 1 and not events(d2, "limit.warn")
    # a jittered resets_at (sub-second / a few seconds) is the same instance
    d3 = run(prof(), snap(session=(88, RESET + timedelta(seconds=0.6))), state=d1.state)
    assert not events(d3, "limit.warn")
    # a new window instance warns again
    d4 = run(prof(), snap(session=(81, RESET + timedelta(hours=5))), state=d1.state)
    assert len(events(d4, "limit.warn")) == 1


@pytest.mark.parametrize("resets", [None, NOW - timedelta(minutes=1)])
def test_inactive_window_creates_nothing(resets: datetime | None) -> None:
    d = run(prof(), snap(session=(99, resets)))
    assert not d.events and not d.state.holds and not pauses(d)


# ---------------------------------------------------------------- model-scoped


def test_model_scoped_pauses_only_matching_sessions() -> None:
    sessions = (sess("opus", model="claude-opus-5-5"), sess("fable", model="claude-fable-5-1"))
    d = run(prof(), snap(scoped=(("Fable", 95, WEEK),)), sessions)
    assert [p.wrapper_id for p in pauses(d)] == ["fable"]
    assert pauses(d)[0].hold_ids == ("model_scoped:Fable",)
    (hold,) = d.state.holds
    assert hold.scope == "Fable" and hold.instance.startswith("model_scoped:Fable:")
    assert events(d, "limit.pause")[0]["sessions_paused"] == 1


def test_model_scoped_unknown_model_not_applicable() -> None:
    d = run(prof(), snap(scoped=(("Fable", 95, WEEK),)), (sess(model=None),))
    assert d.state.holds and not pauses(d)


def test_model_scoped_warn_only_two_warns_no_pause() -> None:
    p = prof({"model_scoped": {"warn": 80, "pause": 95, "warn_only": True}})
    fable = (sess(model="claude-fable-5-1"),)
    d80 = run(p, snap(scoped=(("Fable", 80, WEEK),)), fable)
    assert [e["level"] for e in events(d80, "limit.warn")] == ["warn"]
    d95 = run(p, snap(scoped=(("Fable", 96, WEEK),)), fable, state=d80.state)
    assert [e["level"] for e in events(d95, "limit.warn")] == ["pause_level"]
    assert not d95.state.holds and not pauses(d95)
    fresh = run(p, snap(scoped=(("Fable", 96, WEEK),)), fable)
    assert [e["level"] for e in events(fresh, "limit.warn")] == ["warn", "pause_level"]


# ---------------------------------------------------------------- extra usage / spill


def test_spill_on_with_credits_suppresses_session_pause() -> None:
    p = prof({"extra_usage": {"spill": True, "warn": 80, "pause": 90}})
    d = run(p, snap(session=(95, RESET), extra=credits(10)))
    assert events(d, "limit.warn") and not pauses(d) and not d.state.holds


def test_spill_on_pauses_at_credit_cap() -> None:
    p = prof({"extra_usage": {"spill": True, "warn": 80, "pause": 90}})
    d = run(p, snap(session=(100, RESET), extra=credits(90)))
    assert [h.id for h in d.state.holds] == ["extra_usage"]
    (pa,) = pauses(d)
    assert pa.hold_ids == ("extra_usage",) and pa.resume_at is None
    pe = events(d, "limit.pause")[0]
    assert pe["window"] == "extra_usage" and pe["resume_at"] is None
    extra_warns = [e for e in events(d, "limit.warn") if e["window"] == "extra_usage"]
    assert extra_warns and extra_warns[0]["used"] == 9.0


def test_extra_usage_hold_clears_when_credits_allow() -> None:
    p = prof({"extra_usage": {"spill": True, "warn": 80, "pause": 90}})
    d1 = run(p, snap(extra=credits(92)))
    paused = sess(state="paused", holds=("extra_usage",), busy=True)
    d2 = run(p, snap(extra=credits(40, limit=20.0)), (paused,), state=d1.state)
    assert not d2.state.holds
    assert resumes(d2) == [ResumeSession("w1", PROMPT)]
    assert events(d2, "limit.resume")[0]["reason"] == "credits"


def test_spill_ignored_when_credits_disabled() -> None:
    p = prof({"extra_usage": {"spill": True, "warn": 80, "pause": 90}})
    d = run(p, snap(session=(95, RESET), extra=credits(0, enabled=False)))
    assert [h.id for h in d.state.holds] == ["session"] and pauses(d)


def test_extra_usage_warn_without_spill_no_hold() -> None:
    d = run(prof(), snap(extra=credits(85)))
    (w,) = events(d, "limit.warn")
    assert w["window"] == "extra_usage" and w["percent"] == 85
    assert not d.state.holds


def test_spill_turning_on_releases_session_hold() -> None:
    d1 = run(prof(), snap(session=(95, RESET)))
    p = prof({"extra_usage": {"spill": True, "warn": 80, "pause": 90}})
    paused = sess(state="paused", holds=("session",), busy=True)
    d2 = run(p, snap(session=(96, RESET), extra=credits(5)), (paused,), state=d1.state)
    assert not d2.state.holds and resumes(d2) == [ResumeSession("w1", PROMPT)]
    assert events(d2, "limit.resume")[0]["reason"] == "spill_active"
    # re-armable: spill off again in the same window pauses as normal
    d3 = run(prof(), snap(session=(97, RESET)), (sess(),), state=d2.state)
    assert pauses(d3)


# ---------------------------------------------------------------- hysteresis


def test_same_instance_never_repauses_after_resume() -> None:
    d1 = run(prof(), snap(session=(95, RESET)))
    st = dataclasses.replace(d1.state, holds=())  # e.g. cleared by a manual resume path
    d2 = run(prof(), snap(session=(96, RESET)), state=st)
    assert not pauses(d2) and not d2.state.holds
    d3 = run(prof(), snap(session=(91, RESET + timedelta(hours=5))), state=st)
    assert pauses(d3)  # a new window instance re-arms


def test_released_instance_never_repauses() -> None:
    inst = policy.instance_key("session", RESET)
    st = ProfileSupervisorState(released_instances=(inst,))
    assert not pauses(run(prof(), snap(session=(99, RESET)), state=st))


def test_overridden_session_not_paused_again_for_same_instance() -> None:
    d1 = run(prof(), snap(session=(95, RESET)))
    inst = d1.state.holds[0].instance
    over = sess(state="overridden", overridden=(inst,))
    d2 = run(prof(), snap(session=(97, RESET)), (over,), state=d1.state)
    assert not pauses(d2) and not any(isinstance(a, MarkRunning) for a in d2.actions)


def test_overridden_session_paused_for_new_instance() -> None:
    d1 = run(prof(), snap(session=(95, RESET)))
    inst = d1.state.holds[0].instance
    over = sess(state="overridden", overridden=(inst,))
    d2 = run(prof(), snap(session=(95, RESET), weekly=(96, WEEK)), (over,), state=d1.state)
    (p,) = pauses(d2)
    assert p.hold_ids == ("weekly",) and p.instances == (policy.instance_key("weekly", WEEK),)


def test_overridden_session_marked_running_after_reset() -> None:
    inst = policy.instance_key("session", RESET)
    over = sess(state="overridden", overridden=(inst,))
    d = run(prof(), snap(session=(3, RESET + timedelta(hours=5))), (over,))
    assert d.actions == (MarkRunning("w1"),)


# ---------------------------------------------------------------- data gate / stale


@pytest.mark.parametrize("status", ["stale", "source_error", "needs_sign_in"])
def test_no_new_holds_without_fresh_data(status: str) -> None:
    d = run(prof(), snap(session=(99, RESET), status=status))
    assert not d.state.holds and not d.events and not pauses(d)


def test_no_snapshot_keeps_state() -> None:
    st = ProfileSupervisorState(holds=(session_hold(),))
    paused = sess(state="paused", holds=("session",), resume_at=RESET)
    d = run(prof(), None, (paused,), state=st)
    assert d.state.holds == st.holds and not resumes(d)


def test_stale_hold_time_clears_after_confirmation_window() -> None:
    st = ProfileSupervisorState(holds=(session_hold(),))
    paused = sess(state="paused", holds=("session",), busy=True, resume_at=RESET)
    stale = snap(session=(95, RESET), status="stale")
    start = RESET + timedelta(seconds=15)
    d1 = run(prof(), stale, (paused,), state=st, now=start)
    assert ForcePoll(start) in d1.actions and d1.state.holds[0].confirm_started_at == start
    d2 = run(prof(), stale, (paused,), state=d1.state, now=start + timedelta(minutes=9))
    assert d2.state.holds
    d3 = run(prof(), stale, (paused,), state=d2.state, now=start + timedelta(minutes=10))
    assert not d3.state.holds
    assert any(isinstance(a, LogWarning) for a in d3.actions)
    assert resumes(d3) == [ResumeSession("w1", PROMPT)]
    assert d3.state.released_instances[-1] == session_hold().instance
    assert d3.state.ledger[-2].action == "time_based_clear"
    assert events(d3, "limit.resume")[0]["reason"] == "time_based_clear"


# ---------------------------------------------------------------- reset confirmation


def _confirming(now: datetime) -> tuple[ProfileSupervisorState, SessionView]:
    st = ProfileSupervisorState(holds=(session_hold(),))
    paused = sess(state="paused", holds=("session",), busy=True, resume_at=RESET)
    d = run(prof(), snap(session=(95, RESET), at=RESET - timedelta(minutes=1)), (paused,), st, now)
    return d.state, paused


def test_no_confirmation_before_reset_plus_15s() -> None:
    st = ProfileSupervisorState(holds=(session_hold(),))
    d = run(prof(), snap(session=(95, RESET)), state=st, now=RESET + timedelta(seconds=14))
    assert not any(isinstance(a, ForcePoll) for a in d.actions)
    assert d.state.holds[0].confirm_started_at is None


def test_advanced_window_clears() -> None:
    t = RESET + timedelta(seconds=20)
    st, paused = _confirming(t)
    new = snap(session=(4, RESET + timedelta(hours=5)), at=t)
    d = run(prof(), new, (paused,), state=st, now=t)
    assert not d.state.holds and resumes(d) == [ResumeSession("w1", PROMPT)]
    (re,) = events(d, "limit.resume")
    assert re["reason"] == "reset_confirmed" and re["sessions_resumed"] == 1


def test_percent_below_warn_clears() -> None:
    t = RESET + timedelta(seconds=20)
    st, paused = _confirming(t)
    d = run(prof(), snap(session=(10, RESET + timedelta(seconds=30)), at=t), (paused,), st, t)
    assert not d.state.holds


def test_jittered_same_window_does_not_clear_and_repolls_every_30s() -> None:
    t = RESET + timedelta(seconds=20)
    st, paused = _confirming(t)
    same = snap(session=(95, RESET + timedelta(seconds=0.7)), at=t)
    d1 = run(prof(), same, (paused,), st, t)
    assert d1.state.holds and not any(isinstance(a, ForcePoll) for a in d1.actions)
    d2 = run(prof(), same, (paused,), d1.state, t + timedelta(seconds=10))
    assert not any(isinstance(a, ForcePoll) for a in d2.actions)
    d3 = run(prof(), same, (paused,), d2.state, t + timedelta(seconds=31))
    assert ForcePoll(t + timedelta(seconds=31)) in d3.actions


def test_old_observation_does_not_clear() -> None:
    t = RESET + timedelta(seconds=20)
    st, paused = _confirming(t)
    old = snap(session=(3, RESET + timedelta(hours=5)), at=RESET - timedelta(seconds=5))
    assert run(prof(), old, (paused,), st, t).state.holds


def test_window_gone_after_reset_clears() -> None:
    t = RESET + timedelta(seconds=20)
    st, paused = _confirming(t)
    assert not run(prof(), snap(at=t), (paused,), st, t).state.holds


# ---------------------------------------------------------------- resume prompt


@pytest.mark.parametrize(("busy", "prompt"), [(True, PROMPT), (False, None), (None, None)])
def test_resume_prompt_only_when_busy_at_pause(busy: bool | None, prompt: str | None) -> None:
    paused = sess(state="paused", holds=("session",), busy=busy)
    d = run(prof(), snap(session=(10, RESET)), (paused,))
    assert resumes(d) == [ResumeSession("w1", prompt)]
    assert d.state.ledger[-1].detail == ("prompt" if prompt else "no_prompt")


def test_paused_session_holds_change_updates_record() -> None:
    st = ProfileSupervisorState(holds=(session_hold(),))
    paused = sess(state="paused", holds=("session",), resume_at=RESET)
    d = run(prof(), snap(session=(95, RESET), weekly=(96, WEEK)), (paused,), state=st)
    (u,) = [a for a in d.actions if isinstance(a, UpdateHolds)]
    assert u.hold_ids == ("session", "weekly") and u.resume_at == WEEK


# ---------------------------------------------------------------- supervision off


def test_supervision_disabled_warns_only() -> None:
    p = prof(supervisor={"enabled": False})
    d = run(p, snap(session=(95, RESET)))
    assert events(d, "limit.warn") and not d.state.holds and not pauses(d)


def test_supervision_disabled_releases_existing_holds() -> None:
    p = prof(supervisor={"enabled": False})
    st = ProfileSupervisorState(holds=(session_hold(),))
    paused = sess(state="paused", holds=("session",), busy=True)
    d = run(p, snap(session=(95, RESET)), (paused,), state=st)
    assert not d.state.holds and resumes(d)
    assert events(d, "limit.resume")[0]["reason"] == "supervisor_disabled"


# ---------------------------------------------------------------- manual helpers


def test_manual_hold_idempotent_and_scoped() -> None:
    st, hold, created = policy.add_manual_hold(ProfileSupervisorState(), NOW)
    assert created and hold.scope is None and hold.instance.startswith("manual:")
    st2, same, created2 = policy.add_manual_hold(st, NOW + timedelta(minutes=1))
    assert not created2 and same == hold and st2 == st
    st3, scoped, _ = policy.add_manual_hold(st, NOW, "w9")
    assert scoped.scope == "w9" and len(st3.holds) == 2
    assert policy.applies(scoped, sess("w9")) and not policy.applies(scoped, sess("w1"))
    d = run(prof(), None, (sess("w1"), sess("w9")), state=st3)
    assert {p.wrapper_id: p.hold_ids for p in pauses(d)} == {
        "w1": ("manual",),
        "w9": ("manual",),
    }
    assert all(p.resume_at is None for p in pauses(d))


def test_manual_hold_never_auto_clears() -> None:
    st, _, _ = policy.add_manual_hold(ProfileSupervisorState(), NOW)
    d = run(prof(), snap(session=(1, RESET)), state=st, now=NOW + timedelta(days=1))
    assert d.state.holds


def test_release_holds_marks_instances_released() -> None:
    st = ProfileSupervisorState(holds=(session_hold(),))
    new, cleared = policy.release_holds(st, NOW)
    assert cleared == [session_hold()] and not new.holds
    assert session_hold().instance in new.released_instances
    assert new.ledger[-1].action == "manual_resume"
    assert not pauses(run(prof(), snap(session=(99, RESET)), state=new))


# ---------------------------------------------------------------- labels / helpers


def test_state_label() -> None:
    p = prof()
    assert policy.supervisor_state_label(p, snap(session=(10, RESET)), (), NOW) == "normal"
    assert policy.supervisor_state_label(p, snap(session=(85, RESET)), (), NOW) == "warned"
    assert policy.supervisor_state_label(p, snap(session=(85, RESET)), (session_hold(),), NOW) == (
        "paused"
    )
    assert policy.supervisor_state_label(p, snap(extra=credits(85)), (), NOW) == "warned"
    assert policy.supervisor_state_label(p, None, (), NOW) == "normal"


def test_model_matching() -> None:
    assert policy.model_matches("Fable", "claude-fable-5-1")
    assert not policy.model_matches("Fable", "claude-opus-5-5")
    assert not policy.model_matches("Fable", None)
    assert not policy.model_matches(None, "claude-fable-5-1")


def test_resume_times() -> None:
    manual = Hold(id="manual", kind="manual", instance="manual:x")
    assert policy.latest_resume([session_hold(), manual]) is None
    assert policy.latest_resume([session_hold(), session_hold(WEEK)]) == WEEK
    assert policy.profile_resume_at([session_hold(), manual]) == RESET
    assert policy.profile_resume_at([manual]) is None


def test_evaluate_is_pure() -> None:
    st = ProfileSupervisorState()
    d1 = run(prof(), snap(session=(95, RESET)), state=st)
    d2 = run(prof(), snap(session=(95, RESET)), state=st)
    assert d1 == d2 and st == ProfileSupervisorState()
