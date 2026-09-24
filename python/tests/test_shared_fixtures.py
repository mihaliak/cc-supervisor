"""The Swift test fixtures in `schema/fixtures/` must stay valid against the contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from schema_check import SCHEMA_DIR, validate

SNAPSHOT_FIXTURES = sorted((SCHEMA_DIR / "fixtures" / "snapshot").glob("*.json"))


def test_snapshot_fixtures_exist() -> None:
    names = {p.stem for p in SNAPSHOT_FIXTURES}
    assert {"ok", "stale", "needs_sign_in", "paused", "no_model_scoped", "extra_usage"} <= names


@pytest.mark.parametrize("path", SNAPSHOT_FIXTURES, ids=lambda p: Path(p).stem)
def test_snapshot_fixture_matches_schema(path: Path) -> None:
    doc = json.loads(path.read_text("utf-8"))
    assert validate(doc, "widget-snapshot.schema.json") == []
