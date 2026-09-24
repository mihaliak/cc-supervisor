"""Usage snapshot and live report JSON match `schema/*.schema.json`."""

from __future__ import annotations

from datetime import timedelta

from schema_check import validate
from usage_helpers import T0, payload

from ccs.usage.model import STATUS_NEEDS_SIGN_IN
from ccs.usage.normalize import normalize, snapshot_from_error


def test_snapshots_match_schema() -> None:
    for name in ("ok_max.json", "ok_team.json", "ok_no_window.json", "limits_only_scoped.json"):
        snap = normalize(payload(name), profile_id="p", fetched_at=T0)
        assert validate(snap.to_dict(), "usage-snapshot.schema.json") == [], name
    err = snapshot_from_error("p", STATUS_NEEDS_SIGN_IN, "x", None, T0)
    assert validate(err.to_dict(), "usage-snapshot.schema.json") == []


def test_schema_rejects_bad_snapshot() -> None:
    snap = normalize(payload("ok_max.json"), profile_id="p", fetched_at=T0)
    d = snap.to_dict()
    d["status"] = "weird"
    d["windows"]["session"]["percent"] = 140
    errs = validate(d, "usage-snapshot.schema.json")
    assert any("status" in e for e in errs)
    assert any("session" in e for e in errs)


def test_live_report_examples_match_schema() -> None:
    written_by_statusline = {
        "schema": 1,
        "profile_id": "work",
        "wrapper_id": None,
        "session_id": "00000000-0000-0000-0000-000000000000",
        "model_id": "claude-opus-5-5",
        "effort": None,
        "cwd": "/Users/user/Code/x",
        "observed_at": (T0 + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rate_limits": {"five_hour": {"percent": 18, "resets_at": "2026-09-24T20:00:00Z"}},
    }
    assert validate(written_by_statusline, "live-report.schema.json") == []
    # the reader also tolerates the raw statusline aliases
    tolerant = {
        **written_by_statusline,
        "rate_limits": {"seven_day": {"used_percentage": 3.5, "resets_at": 1790280000}},
    }
    assert validate(tolerant, "live-report.schema.json") == []
    assert validate({"schema": 1}, "live-report.schema.json") != []
