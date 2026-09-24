"""Shared helpers for the usage tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ccs.config.models import Profile

FIXTURES = Path(__file__).parent / "fixtures" / "get_usage"
T0 = datetime(2026, 9, 24, 16, 7, 33, tzinfo=UTC)


def payload(name: str) -> dict[str, Any]:
    """The `response.response` object of a captured get_usage fixture."""
    data = json.loads((FIXTURES / name).read_text("utf-8"))
    inner = data["response"]["response"]
    assert isinstance(inner, dict)
    return inner


def make_profile(config_dir: Path | str, pid: str = "work") -> Profile:
    return Profile.from_dict(
        {"id": pid, "flag": pid, "name": pid.title(), "emoji": "💼", "config_dir": str(config_dir)}
    )
