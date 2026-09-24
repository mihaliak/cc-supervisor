"""Widget snapshot builder: levels, rows, extra usage detail, supervisor fields, schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schema_check import validate

from ccs.config.models import Config
from ccs.snapshot import (
    build_widget_snapshot,
    format_money,
    level_for,
    worst_level,
)
from ccs.usage.model import (
    ExtraUsage,
    ScopedWindow,
    UsageSnapshot,
    Window,
)

NOW = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)


def config(spill: bool = False) -> Config:
    return Config.from_dict(
        {
            "version": 1,
            "revision": 0,
            "default_profile": "work",
            "profiles": [
                {
                    "id": "work",
                    "flag": "work",
                    "name": "Work",
                    "emoji": "💼",
                    "config_dir": "~/w",
                    "limits": {"extra_usage": {"spill": spill, "warn": 80, "pause": 90}},
                },
                {"id": "home", "flag": "home", "name": "Home", "emoji": "🏠", "config_dir": "~/h"},
            ],
        }
    )


def snap(extra: ExtraUsage | None = None, status: str = "ok") -> UsageSnapshot:
    return UsageSnapshot(
        profile_id="work",
        status=status,
        fetched_at=NOW,
        polled_at=NOW,
        session=Window(45, NOW + timedelta(hours=4), NOW),
        weekly=Window(86, NOW + timedelta(days=2), NOW),
        model_scoped=(ScopedWindow("Fable", 4, NOW + timedelta(days=2), NOW),),
        extra_usage=extra,
    )


def build(config_: Config, snapshots: dict[str, UsageSnapshot | None], **kw: Any) -> dict[str, Any]:
    return build_widget_snapshot(
        config_,
        snapshots,
        kw.get("supervisor", {}),
        kw.get("sessions", {}),
        kw.get("other", {}),
        kw.get("warmups", {}),
        NOW,
    )


@pytest.mark.parametrize(
    ("percent", "level"), [(0, "green"), (49, "green"), (50, "yellow"), (79, "yellow"), (80, "red")]
)
def test_level_boundaries(percent: int, level: str) -> None:
    assert level_for(percent, config().display.colors) == level


def test_worst_level() -> None:
    assert worst_level(["green", "red", "yellow"]) == "red"
    assert worst_level(["green", "yellow"]) == "yellow"
    assert worst_level([]) is None


def test_rows_order_and_levels() -> None:
    doc = build(config(), {"work": snap()})
    work = doc["profiles"][0]
    assert [r["kind"] for r in work["rows"]] == ["session", "weekly", "model_scoped"]
    assert [r["label"] for r in work["rows"]] == ["Session", "Weekly", "Fable"]
    assert [r["level"] for r in work["rows"]] == ["green", "red", "green"]
    assert work["level"] == "red"
    assert work["rows"][0]["resets_at"] == "2026-09-24T20:00:00Z"
    assert work["updated_at"] == "2026-09-24T16:00:00Z"
    assert doc["generated_at"] == "2026-09-24T16:00:00Z"


def test_no_data_profile() -> None:
    doc = build(config(), {"work": snap()})
    home = doc["profiles"][1]
    assert home["status"] == "no_data"
    assert home["rows"] == [] and home["level"] is None and home["updated_at"] is None


def test_extra_usage_row_when_enabled() -> None:
    extra = ExtraUsage(True, 32, 3.2, 10.0, "EUR", None)
    row = build(config(), {"work": snap(extra)})["profiles"][0]["rows"][-1]
    assert row == {
        "kind": "extra_usage",
        "label": "Extra usage",
        "percent": 32,
        "level": "green",
        "resets_at": None,
        "detail": "€3.20 / €10.00",
    }


def test_extra_usage_row_hidden_when_disabled_without_spill() -> None:
    extra = ExtraUsage(False, 0, 0.0, 10.0, "EUR", "out_of_credits")
    kinds = [r["kind"] for r in build(config(), {"work": snap(extra)})["profiles"][0]["rows"]]
    assert "extra_usage" not in kinds
    kinds = [
        r["kind"] for r in build(config(spill=True), {"work": snap(extra)})["profiles"][0]["rows"]
    ]
    assert kinds[-1] == "extra_usage"


@pytest.mark.parametrize(
    ("value", "currency", "text"),
    [(3.2, "EUR", "€3.20"), (3.2, "USD", "$3.20"), (3.2, "CHF", "3.20 CHF"), (None, None, "0.00")],
)
def test_format_money(value: float | None, currency: str | None, text: str) -> None:
    assert format_money(value, currency) == text


def test_stale_flag() -> None:
    doc = build(config(), {"work": snap(status="stale")})
    assert doc["profiles"][0]["stale"] is True and doc["profiles"][0]["status"] == "stale"


def test_supervisor_defaults_and_contributions() -> None:
    sessions = {
        "work": [
            {"wrapper_id": "a", "supervision": {"state": "paused"}},
            {"wrapper_id": "b", "supervision": {"state": "running"}},
        ]
    }
    doc = build(
        config(),
        {"work": snap()},
        sessions=sessions,
        other={"work": {"interactive": 2, "background": 1}},
        warmups={"work": NOW + timedelta(hours=1)},
    )
    sup = doc["profiles"][0]["supervisor"]
    assert sup == {
        "state": "paused",
        "active_sessions": 2,
        "paused_sessions": 1,
        "other_sessions": 3,
        "resume_at": None,
        "next_warmup_at": "2026-09-24T17:00:00Z",
    }
    contributed = build(
        config(),
        {"work": snap()},
        supervisor={"work": {"state": "warned", "resume_at": NOW, "holds": ["x"]}},
    )["profiles"][0]["supervisor"]
    assert contributed["state"] == "warned"
    assert contributed["resume_at"] == "2026-09-24T16:00:00Z"
    assert "holds" not in contributed  # only the snapshot keys are published


def test_document_matches_schema() -> None:
    extra = ExtraUsage(True, 32, 3.2, 10.0, "EUR", None)
    doc = build(config(), {"work": snap(extra)})
    assert validate(doc, "widget-snapshot.schema.json") == []
