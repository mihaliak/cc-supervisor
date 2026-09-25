from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from usage_helpers import T0, payload

from ccs.usage.model import (
    STATUS_NEEDS_SIGN_IN,
    STATUS_OK,
    STATUS_SOURCE_ERROR,
    UsageSnapshot,
    parse_time,
    round_percent,
)
from ccs.usage.normalize import (
    MalformedPayload,
    normalize,
    parse_window_key_time,
    session_window_active,
    snapshot_from_error,
    window_key_time,
)


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_ok_max_fixture() -> None:
    snap = normalize(payload("ok_max.json"), profile_id="personal", fetched_at=T0)
    assert snap.status == STATUS_OK
    assert snap.subscription_type == "max"
    assert snap.fetched_at == snap.polled_at == T0
    assert snap.session is not None and snap.session.percent == 15
    # sub-second jitter (…20:00:00.742595) truncated to whole seconds
    assert snap.session.resets_at == utc(2026, 9, 24, 20, 0, 0)
    assert snap.session.source == "get_usage"
    assert snap.session.observed_at == T0
    assert snap.weekly is not None and snap.weekly.percent == 52
    assert snap.weekly.resets_at == utc(2026, 9, 26, 6, 0, 0)
    assert [(w.name, w.percent) for w in snap.model_scoped] == [("Fable", 4)]
    extra = snap.extra_usage
    assert extra is not None
    assert extra.enabled is False
    assert extra.used == 0.0 and extra.limit == 10.0
    assert extra.currency == "EUR" and extra.percent == 0
    assert extra.disabled_reason == "out_of_credits"


def test_ok_team_fixture_has_null_extra_fields() -> None:
    snap = normalize(payload("ok_team.json"), profile_id="work", fetched_at=T0)
    assert snap.subscription_type == "team"
    assert snap.session is not None and snap.session.percent == 37
    assert snap.weekly is not None and snap.weekly.percent == 70
    assert [(w.name, w.percent) for w in snap.model_scoped] == [("Fable", 0)]
    extra = snap.extra_usage
    assert extra is not None
    assert (extra.enabled, extra.used, extra.limit, extra.percent, extra.currency) == (
        False,
        None,
        None,
        None,
        None,
    )


def test_no_window_fixture_is_inactive() -> None:
    snap = normalize(payload("ok_no_window.json"), profile_id="work", fetched_at=T0)
    assert snap.session is None
    assert snap.weekly is not None
    assert session_window_active(snap, T0) is False


def test_session_window_active_rules() -> None:
    snap = normalize(payload("ok_max.json"), profile_id="p", fetched_at=T0)
    assert session_window_active(snap, T0) is True
    assert session_window_active(snap, utc(2026, 9, 24, 20, 0, 0)) is False  # resets_at == now
    assert session_window_active(snap, utc(2026, 9, 24, 21, 0)) is False
    assert session_window_active(None, T0) is False
    raw = copy.deepcopy(payload("ok_max.json"))
    raw["rate_limits"]["five_hour"]["resets_at"] = None
    assert session_window_active(normalize(raw, profile_id="p", fetched_at=T0), T0) is False


def test_limits_only_scoped_fallback() -> None:
    raw = payload("limits_only_scoped.json")
    assert "model_scoped" not in raw["rate_limits"]
    snap = normalize(raw, profile_id="p", fetched_at=T0)
    assert [(w.name, w.percent, w.resets_at) for w in snap.model_scoped] == [
        ("Fable", 4, utc(2026, 9, 26, 6, 0, 0))
    ]


def test_primary_model_scoped_wins_and_dedupes() -> None:
    raw = copy.deepcopy(payload("ok_max.json"))
    raw["rate_limits"]["model_scoped"] = [
        {"display_name": "Fable", "utilization": 7, "resets_at": None},
        {"display_name": "Fable", "utilization": 9, "resets_at": None},
    ]
    snap = normalize(raw, profile_id="p", fetched_at=T0)
    assert [(w.name, w.percent) for w in snap.model_scoped] == [("Fable", 7)]


def test_empty_model_scoped_list_means_none() -> None:
    raw = copy.deepcopy(payload("ok_max.json"))
    raw["rate_limits"]["model_scoped"] = []
    assert normalize(raw, profile_id="p", fetched_at=T0).model_scoped == ()


@pytest.mark.parametrize(
    ("utilization", "expected"),
    [(44.4, 44), (44.5, 45), (0.49, 0), (99.5, 100), (100.7, 100), (-3, 0), (15, 15)],
)
def test_utilization_rounding(utilization: float, expected: int) -> None:
    raw = copy.deepcopy(payload("ok_max.json"))
    raw["rate_limits"]["five_hour"]["utilization"] = utilization
    snap = normalize(raw, profile_id="p", fetched_at=T0)
    assert snap.session is not None and snap.session.percent == expected


def test_null_utilization_drops_window() -> None:
    raw = copy.deepcopy(payload("ok_max.json"))
    raw["rate_limits"]["seven_day"]["utilization"] = None
    assert normalize(raw, profile_id="p", fetched_at=T0).weekly is None


@pytest.mark.parametrize(
    ("dp", "limit", "used", "util", "expected"),
    [
        (2, 1000, 320, None, (10.0, 3.2, 32)),
        (0, 10, 3, None, (10.0, 3.0, 30)),
        (2, 1000, 1250, 125, (10.0, 12.5, 125)),  # extra usage may exceed 100
        (None, 500, 100, 20, (5.0, 1.0, 20)),  # decimal_places missing → 2
    ],
)
def test_extra_usage_minor_units(
    dp: int | None, limit: int, used: int, util: int | None, expected: tuple[float, float, int]
) -> None:
    raw = copy.deepcopy(payload("ok_max.json"))
    extra = raw["rate_limits"]["extra_usage"]
    extra.update(
        {"is_enabled": True, "decimal_places": dp, "monthly_limit": limit, "used_credits": used}
    )
    extra["utilization"] = util
    snap = normalize(raw, profile_id="p", fetched_at=T0)
    assert snap.extra_usage is not None
    assert snap.extra_usage.enabled is True
    assert (snap.extra_usage.limit, snap.extra_usage.used, snap.extra_usage.percent) == expected


def test_missing_extra_usage_is_none() -> None:
    raw = copy.deepcopy(payload("ok_max.json"))
    del raw["rate_limits"]["extra_usage"]
    assert normalize(raw, profile_id="p", fetched_at=T0).extra_usage is None


@pytest.mark.parametrize(
    "bad",
    [
        {"rate_limits": {"five_hour": "x"}},
        {"rate_limits": None},
        {},
        {"rate_limits": {"five_hour": {"utilization": "high"}}},
        {"rate_limits": {"seven_day": {"utilization": 3, "resets_at": "not a date"}}},
        {"rate_limits": {"model_scoped": {"Fable": 1}}},
    ],
)
def test_malformed_payloads_raise(bad: dict[str, object]) -> None:
    with pytest.raises(MalformedPayload):
        normalize(bad, profile_id="p", fetched_at=T0)


def test_window_key_time_absorbs_jitter() -> None:
    a = parse_time("2026-09-24T20:00:00.326066+00:00")
    b = parse_time("2026-09-24T20:00:00.742595+00:00")
    c = parse_time("2026-09-24T19:59:58+00:00")
    assert a is not None and b is not None and c is not None
    assert window_key_time(a) == window_key_time(b) == window_key_time(c) == "2026-09-24T20:00Z"
    assert window_key_time(utc(2026, 9, 24, 20, 0, 31)) == "2026-09-24T20:01Z"
    assert window_key_time(utc(2026, 9, 24, 23, 59, 45)) == "2026-09-25T00:00Z"


def test_parse_window_key_time_round_trips() -> None:
    key = window_key_time(utc(2026, 9, 24, 20, 0, 29))
    assert parse_window_key_time(key) == utc(2026, 9, 24, 20, 0)
    assert parse_window_key_time("2026-09-24T20:00") is None
    assert parse_window_key_time("manual:x") is None


def test_parse_time_variants() -> None:
    assert parse_time("2026-09-24T20:00:00Z") == utc(2026, 9, 24, 20, 0)
    assert parse_time("2026-09-24T22:00:00+02:00") == utc(2026, 9, 24, 20, 0)
    assert parse_time(1790280000) == datetime.fromtimestamp(1790280000, UTC)
    assert parse_time(1790280000.9) == datetime.fromtimestamp(1790280000, UTC)
    for bad in (None, "", "soon", True, float("nan"), [], {}):
        assert parse_time(bad) is None


def test_round_percent_rejects_non_numbers() -> None:
    assert round_percent(True) is None
    assert round_percent("5") is None
    assert round_percent(float("inf")) is None
    assert round_percent(250, clamp_max=None) == 250


def test_snapshot_from_error_keeps_previous_windows() -> None:
    prev = normalize(payload("ok_max.json"), profile_id="p", fetched_at=T0)
    later = T0 + timedelta(minutes=3)
    snap = snapshot_from_error("p", STATUS_SOURCE_ERROR, "boom", prev, later)
    assert snap.status == STATUS_SOURCE_ERROR and snap.error == "boom"
    assert snap.fetched_at == T0 and snap.polled_at == later
    assert snap.session == prev.session and snap.model_scoped == prev.model_scoped


def test_snapshot_from_error_without_previous() -> None:
    snap = snapshot_from_error("p", STATUS_NEEDS_SIGN_IN, "x", None, T0)
    assert snap == UsageSnapshot(
        profile_id="p", status=STATUS_NEEDS_SIGN_IN, error="x", polled_at=T0
    )


def test_snapshot_from_error_ignores_foreign_previous() -> None:
    prev = normalize(payload("ok_max.json"), profile_id="other", fetched_at=T0)
    assert snapshot_from_error("p", STATUS_SOURCE_ERROR, "e", prev, T0).session is None


def test_snapshot_round_trip() -> None:
    snap = normalize(payload("ok_max.json"), profile_id="p", fetched_at=T0)
    d = snap.to_dict()
    assert d["schema"] == 1
    assert d["windows"]["session"]["resets_at"] == "2026-09-24T20:00:00Z"
    assert UsageSnapshot.from_dict(d) == snap


@pytest.mark.parametrize("bad", [None, [], {"profile_id": "p"}, {"profile_id": "", "status": "ok"}])
def test_from_dict_rejects_garbage(bad: object) -> None:
    assert UsageSnapshot.from_dict(bad) is None
