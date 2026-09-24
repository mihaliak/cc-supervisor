"""Warm-up skip rules: each rule alone, precedence, `force`, active hours (ADR-0010)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from warmup_helpers import TZ, local, profile, snapshot

from ccs.warmup import rules
from ccs.warmup.rules import WarmupContext, evaluate, in_active_hours, parse_hhmm

NOW = local(2026, 9, 24, 12)  # Thu 12:00 local


def ctx(**over: Any) -> WarmupContext:
    base: dict[str, Any] = {
        "profile": profile(),
        "trigger": rules.MANUAL,
        "now": NOW,
        "snapshot": snapshot(session=False),
        "sessions": (),
        "holds": (),
        "last_attempt_at": None,
        "force": False,
        "catch_up": False,
        "tz": TZ,
    }
    base.update(over)
    return WarmupContext(**base)


ACTIVE = snapshot(resets_at=NOW + timedelta(hours=2))
BUSY = ({"activity": "busy"},)
RECENT = NOW - timedelta(minutes=3)
NIGHT = local(2026, 9, 24, 3)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({}, None),
        ({"profile": profile(enabled=False)}, rules.DISABLED),
        (
            {"trigger": rules.APP_START, "profile": profile(triggers={"app_start": False})},
            rules.DISABLED,
        ),
        (
            {"trigger": rules.UNLOCK_WAKE, "profile": profile(triggers={"unlock_wake": False})},
            rules.DISABLED,
        ),
        (
            {"trigger": rules.AUTO_CHAIN, "profile": profile(triggers={"auto_chain": False})},
            rules.DISABLED,
        ),
        ({"trigger": rules.SCHEDULE}, rules.DISABLED),  # empty schedule list
        ({"snapshot": snapshot(status="needs_sign_in", session=False)}, rules.NEEDS_SIGN_IN),
        ({"snapshot": ACTIVE}, rules.WINDOW_ACTIVE),
        ({"sessions": BUSY}, rules.SESSION_BUSY),
        ({"sessions": ({"activity": "shell"},)}, rules.SESSION_BUSY),
        ({"sessions": ({"activity": "waiting"},)}, rules.SESSION_BUSY),
        ({"sessions": ({"activity": "idle"}, {"activity": "unknown"}, {})}, None),
        ({"last_attempt_at": RECENT}, rules.COOLDOWN),
        ({"last_attempt_at": NOW - timedelta(minutes=10)}, None),  # cooldown boundary
        ({"trigger": rules.AUTO_CHAIN, "now": NIGHT}, rules.OUTSIDE_ACTIVE_HOURS),
        (
            {
                "trigger": rules.SCHEDULE,
                "catch_up": True,
                "now": NIGHT,
                "profile": profile(triggers={"schedule": [{"time": "03:00", "weekdays": ["thu"]}]}),
            },
            rules.OUTSIDE_ACTIVE_HOURS,
        ),
        (
            {
                "trigger": rules.SCHEDULE,
                "now": NIGHT,
                "profile": profile(triggers={"schedule": [{"time": "03:00", "weekdays": ["thu"]}]}),
            },
            None,
        ),  # regular schedules fire outside active hours
        ({"trigger": rules.APP_START, "now": NIGHT}, None),
        ({"holds": ("weekly",)}, rules.WEEKLY_HOLD),
        ({"holds": ("session", "model_scoped:Fable")}, None),
        ({"snapshot": snapshot(resets_at=NOW - timedelta(minutes=1))}, None),  # past = inactive
        ({"snapshot": snapshot(resets_at=None)}, None),
        ({"snapshot": None}, None),
    ],
)
def test_each_rule(over: dict[str, Any], reason: str | None) -> None:
    decision = evaluate(ctx(**over))
    assert decision.reason == reason
    assert decision.run is (reason is None)


def test_precedence_first_rule_wins() -> None:
    everything = {
        "snapshot": snapshot(status="needs_sign_in", resets_at=NOW + timedelta(hours=1)),
        "sessions": BUSY,
        "last_attempt_at": RECENT,
        "holds": ("weekly",),
    }
    assert evaluate(ctx(**everything, profile=profile(enabled=False))).reason == rules.DISABLED
    assert evaluate(ctx(**everything)).reason == rules.NEEDS_SIGN_IN
    everything["snapshot"] = ACTIVE
    assert evaluate(ctx(**everything)).reason == rules.WINDOW_ACTIVE
    everything["snapshot"] = None
    assert evaluate(ctx(**everything)).reason == rules.SESSION_BUSY
    everything["sessions"] = ()
    assert evaluate(ctx(**everything)).reason == rules.COOLDOWN
    everything["last_attempt_at"] = None
    assert evaluate(ctx(**everything, trigger=rules.AUTO_CHAIN, now=NIGHT)).reason == (
        rules.OUTSIDE_ACTIVE_HOURS
    )
    assert evaluate(ctx(**everything)).reason == rules.WEEKLY_HOLD


def test_force_bypasses_3_to_7_only() -> None:
    blocked = {
        "snapshot": ACTIVE,
        "sessions": BUSY,
        "last_attempt_at": RECENT,
        "holds": ("weekly",),
        "trigger": rules.AUTO_CHAIN,
        "now": NIGHT,
    }
    assert evaluate(ctx(**blocked, force=True)).run
    assert evaluate(ctx(force=True, profile=profile(enabled=False))).reason == rules.DISABLED
    signed_out = snapshot(status="needs_sign_in", session=False)
    assert evaluate(ctx(force=True, snapshot=signed_out)).reason == rules.NEEDS_SIGN_IN


def test_manual_has_no_toggle() -> None:
    off = profile(
        triggers={"app_start": False, "unlock_wake": False, "auto_chain": False, "schedule": []}
    )
    assert evaluate(ctx(profile=off, trigger=rules.MANUAL)).run
    assert not rules.trigger_enabled(off, "bogus")


@pytest.mark.parametrize(
    ("hm", "start", "end", "inside"),
    [
        ("07:00", "07:00", "23:00", True),  # start inclusive
        ("22:59", "07:00", "23:00", True),
        ("23:00", "07:00", "23:00", False),  # end exclusive
        ("06:59", "07:00", "23:00", False),
        ("23:30", "22:00", "06:00", True),  # wraps past midnight
        ("02:00", "22:00", "06:00", True),
        ("06:00", "22:00", "06:00", False),
        ("12:00", "22:00", "06:00", False),
        ("12:00", "00:00", "00:00", True),  # start == end → whole day
    ],
)
def test_in_active_hours(hm: str, start: str, end: str, inside: bool) -> None:
    h, m = parse_hhmm(hm)
    now = datetime(2026, 9, 24, h, m, tzinfo=TZ).astimezone(UTC)
    assert in_active_hours(now, start, end, TZ) is inside


@pytest.mark.parametrize("bad", ["7:00", "24:00", "12:60", "ab:cd", "1200", ""])
def test_parse_hhmm_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_hhmm(bad)
