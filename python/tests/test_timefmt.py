from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ccs.timefmt import (
    format_relative,
    format_reset_absolute,
    format_reset_combined,
    format_reset_compact,
)

VECTORS_PATH = Path(__file__).resolve().parents[2] / "schema" / "fixtures" / "time_format.json"
VECTORS: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_enough_vectors() -> None:
    assert len(VECTORS) >= 20


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_vector(vec: dict[str, Any]) -> None:
    tz = ZoneInfo(vec["tz"])
    now, reset = _dt(vec["now"]), _dt(vec["reset"])
    assert format_reset_absolute(reset, now, tz) == vec["absolute"]
    assert format_relative(reset, now) == vec["relative"]
    assert format_reset_combined(reset, now, tz) == vec["combined"]
    assert format_reset_compact(reset, now, tz) == vec["compact"]


def test_range_edge_does_not_overflow() -> None:
    """Minute rounding or the local zone would step past `datetime.max`: shown as given."""
    tz = ZoneInfo("Europe/Bratislava")
    now = _dt("2026-09-24T15:47:00Z")
    edge = _dt("9999-12-31T23:59:59Z")
    assert format_reset_absolute(edge, now, tz) == "31 Dec 23:59"
    assert format_reset_combined(edge, now, tz).startswith("31 Dec 23:59 (in ")
