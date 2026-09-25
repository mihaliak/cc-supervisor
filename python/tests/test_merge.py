from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

from usage_helpers import T0, payload

from ccs.usage.merge import LiveReport, apply_staleness, merge, parse_live_report
from ccs.usage.model import STATUS_OK, STATUS_SOURCE_ERROR, STATUS_STALE, UsageSnapshot
from ccs.usage.normalize import normalize

RESET = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)


def report(
    minutes_after: float, five: int | None = 60, seven: int | None = None, **extra: Any
) -> LiveReport:
    rl: dict[str, Any] = {}
    if five is not None:
        rl["five_hour"] = {"percent": five, "resets_at": "2026-09-24T20:00:00Z"}
    if seven is not None:
        rl["seven_day"] = {"percent": seven, "resets_at": "2026-09-26T06:00:00Z"}
    d = {
        "schema": 1,
        "profile_id": "p",
        "wrapper_id": "w1",
        "session_id": "s1",
        "model_id": "claude-opus-5-5",
        "effort": "xhigh",
        "cwd": "/tmp",
        "observed_at": (T0 + timedelta(minutes=minutes_after)).isoformat(),
        "rate_limits": rl,
        **extra,
    }
    parsed = parse_live_report(d)
    assert parsed is not None
    return parsed


def base() -> UsageSnapshot:
    return normalize(payload("ok_max.json"), profile_id="p", fetched_at=T0)


def test_newer_report_wins_for_session_only() -> None:
    now = T0 + timedelta(minutes=2)
    merged = merge(base(), [report(1, five=61)], now=now)
    assert merged is not None
    assert merged.session is not None
    assert (merged.session.percent, merged.session.source) == (61, "statusline")
    assert merged.session.observed_at == T0 + timedelta(minutes=1)
    assert merged.session.resets_at == RESET
    assert merged.weekly == base().weekly  # report had no seven_day
    assert merged.model_scoped == base().model_scoped
    assert merged.extra_usage == base().extra_usage


def test_weekly_from_report() -> None:
    merged = merge(base(), [report(1, five=None, seven=77)], now=T0 + timedelta(minutes=2))
    assert merged is not None and merged.weekly is not None
    assert merged.weekly.percent == 77 and merged.session == base().session


def test_older_report_of_another_window_loses() -> None:
    later = dataclasses.replace(base())
    older = report(-1, five=99)
    assert older.five_hour is not None
    other = dataclasses.replace(
        older,
        five_hour=dataclasses.replace(older.five_hour, resets_at=RESET + timedelta(hours=1)),
    )
    merged = merge(later, [other], now=T0 + timedelta(minutes=1))
    assert merged == later


def test_newer_report_of_another_window_wins_with_a_lower_percent() -> None:
    newer = report(1, five=3)
    assert newer.five_hour is not None
    fresh = dataclasses.replace(
        newer,
        five_hour=dataclasses.replace(newer.five_hour, resets_at=RESET + timedelta(hours=5)),
    )
    merged = merge(base(), [fresh], now=T0 + timedelta(minutes=2))
    assert merged is not None and merged.session is not None
    assert (merged.session.percent, merged.session.resets_at) == (3, RESET + timedelta(hours=5))


def test_same_window_higher_percent_wins_even_when_older() -> None:
    # ADR-0022: usage only rises within a window instance
    merged = merge(base(), [report(-1, five=99)], now=T0 + timedelta(minutes=1))
    assert merged is not None and merged.session is not None
    assert (merged.session.percent, merged.session.source) == (99, "statusline")
    assert merged.session.observed_at == T0 - timedelta(minutes=1)


def test_same_window_stale_redraw_cannot_lower_the_poll() -> None:
    polled = merge(base(), [report(1, five=40)], now=T0 + timedelta(minutes=2))
    assert polled is not None and polled.session is not None
    # a statusline redraw with an old, lower percent (resets_at jittered by seconds)
    lower = report(3, five=20)
    assert lower.five_hour is not None
    jitter = dataclasses.replace(
        lower,
        five_hour=dataclasses.replace(lower.five_hour, resets_at=RESET + timedelta(seconds=2)),
    )
    merged = merge(polled, [jitter], now=T0 + timedelta(minutes=4))
    assert merged is not None and merged.session == polled.session


def test_same_window_equal_percent_keeps_newest_observation() -> None:
    merged = merge(base(), [report(1, five=15)], now=T0 + timedelta(minutes=2))
    assert merged is not None and merged.session is not None
    assert merged.session.observed_at == T0 + timedelta(minutes=1)


def test_newest_of_several_reports_wins() -> None:
    reports = [report(3, five=70), report(1, five=65), report(2, five=68)]
    merged = merge(base(), reports, now=T0 + timedelta(minutes=4))
    assert merged is not None and merged.session is not None
    assert merged.session.percent == 70


def test_stale_report_ignored() -> None:
    now = T0 + timedelta(minutes=15)
    merged = merge(base(), [report(4, five=80)], now=now)  # 11 min old
    assert merged == base()


def test_report_for_other_profile_ignored() -> None:
    other = dataclasses.replace(report(1, five=80), profile_id="q")
    assert merge(base(), [other], now=T0 + timedelta(minutes=2)) == base()


def test_report_for_ended_window_ignored() -> None:
    now = RESET + timedelta(minutes=1)
    rep = report(0, five=95)
    rep = dataclasses.replace(rep, observed_at=now - timedelta(seconds=30))
    merged = merge(base(), [rep], now=now)
    assert merged == base()


def test_reports_without_snapshot() -> None:
    now = T0 + timedelta(minutes=2)
    merged = merge(None, [report(1, five=12)], now=now, profile_id="p")
    assert merged is not None
    assert merged.status == STATUS_OK and merged.fetched_at is None
    assert merged.session is not None and merged.session.percent == 12
    assert merge(None, [], now=now, profile_id="p") is None
    # without a snapshot the caller must name the profile
    assert merge(None, [report(1)], now=now) is None


def test_error_snapshot_keeps_status_and_takes_live_windows() -> None:
    errored = dataclasses.replace(base(), status=STATUS_SOURCE_ERROR, error="boom")
    merged = merge(errored, [report(1, five=40)], now=T0 + timedelta(minutes=2))
    assert merged is not None
    assert merged.status == STATUS_SOURCE_ERROR
    assert merged.session is not None and merged.session.percent == 40


def test_parse_live_report_tolerates_raw_statusline_fields() -> None:
    parsed = parse_live_report(
        {
            "observed_at": 1790270000,
            "effort": {"level": "low"},
            "rate_limits": {
                "five_hour": {"used_percentage": 18.4, "resets_at": 1790280000},
                "seven_day": {"used_percentage": "x"},
            },
            "unknown": 1,
        }
    )
    assert parsed is not None
    assert parsed.effort == "low" and parsed.profile_id is None
    assert parsed.five_hour is not None and parsed.five_hour.percent == 18
    assert parsed.five_hour.resets_at == datetime.fromtimestamp(1790280000, UTC)
    assert parsed.seven_day is None


def test_parse_live_report_requires_observed_at() -> None:
    assert parse_live_report({"rate_limits": {}}) is None
    assert parse_live_report("nope") is None
    assert parse_live_report({"observed_at": "2026-09-24T16:00:00Z"}) is not None


def test_staleness_at_exactly_ten_minutes() -> None:
    snap = base()
    assert apply_staleness(snap, T0 + timedelta(minutes=10)).status == STATUS_OK
    assert apply_staleness(snap, T0 + timedelta(minutes=10, seconds=1)).status == STATUS_STALE


def test_live_observation_keeps_snapshot_fresh() -> None:
    now = T0 + timedelta(minutes=14)
    merged = merge(base(), [report(12, five=50)], now=now)
    assert merged is not None
    assert apply_staleness(merged, now).status == STATUS_OK


def test_staleness_only_for_ok() -> None:
    errored = dataclasses.replace(base(), status=STATUS_SOURCE_ERROR)
    assert apply_staleness(errored, T0 + timedelta(hours=1)).status == STATUS_SOURCE_ERROR
    empty = UsageSnapshot(profile_id="p", status=STATUS_OK)
    assert apply_staleness(empty, T0).status == STATUS_STALE
