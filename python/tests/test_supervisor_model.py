"""Supervisor state (de)serialization and bounds (P06)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schema_check import validate

from ccs.supervisor import ledger
from ccs.supervisor.model import (
    BOUND_KEYS,
    Hold,
    ProfileSupervisorState,
    SessionView,
    bounded,
)

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)


def test_round_trip_and_schema() -> None:
    hold = Hold(
        id="model_scoped:Fable",
        kind="model_scoped",
        instance="model_scoped:Fable:2026-09-26T06:00Z",
        scope="Fable",
        resets_at=NOW + timedelta(days=2),
        created_at=NOW,
        confirm_started_at=NOW,
        next_confirm_poll_at=NOW + timedelta(seconds=30),
    )
    st = ProfileSupervisorState(
        holds=(hold,),
        warned=("warn:x",),
        paused_instances=("model_scoped:Fable:2026-09-26T06:00Z",),
        released_instances=("session:2026-09-24T20:00Z",),
        ledger=(ledger.entry(NOW, "pause", instance="i", wrapper_ids=["w1"]),),
    )
    doc = st.to_dict("work")
    assert validate(doc, "supervisor-state.schema.json") == []
    assert ProfileSupervisorState.from_dict(doc) == st


def test_tolerant_parse() -> None:
    assert ProfileSupervisorState.from_dict(None) == ProfileSupervisorState()
    doc = {"holds": [{"id": "x"}, {"id": "manual", "instance": "manual:1"}, 5], "ledger": [{}]}
    st = ProfileSupervisorState.from_dict(doc)
    assert [h.kind for h in st.holds] == ["manual"] and st.ledger == ()


def test_bounds() -> None:
    keys = [f"k{i}" for i in range(BOUND_KEYS + 10)]
    out = bounded(keys)
    assert len(out) == BOUND_KEYS and out[-1] == keys[-1] and out[0] == "k10"
    assert bounded(["a", "b", "a"]) == ("b", "a")
    entries = [ledger.entry(NOW, "resume") for _ in range(250)]
    assert len(ledger.append((), entries)) == 200
    assert ledger.last(entries, "resume") is entries[-1]
    assert ledger.last(entries, "pause") is None


def test_ledger_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        ledger.entry(NOW, "explode")


def test_session_view_from_record() -> None:
    rec = {
        "wrapper_id": "w1",
        "model_id": "claude-fable-5-1",
        "activity": "busy",
        "supervision": {
            "state": "paused",
            "holds": ["session", 3],
            "was_busy_at_pause": None,
            "overridden_instances": ["x"],
            "resume_at": "2026-09-24T20:00:00Z",
        },
    }
    view = SessionView.from_record(rec)
    assert view is not None
    assert view.supervision.state == "paused" and view.supervision.holds == ("session",)
    assert view.supervision.was_busy_at_pause is None
    assert view.supervision.resume_at == datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    assert SessionView.from_record({}) is None
    bad = SessionView.from_record({"wrapper_id": "w", "supervision": {"state": "weird"}})
    assert bad is not None and bad.supervision.state == "running"
