"""The P11 Swift tests rely on these shared fixtures matching what Python really writes/reports."""

from __future__ import annotations

import json
from pathlib import Path

from ccs.config.validate import validate
from ccs.fsio import dumps_json

FIXTURES = Path(__file__).resolve().parents[2] / "schema" / "fixtures"


def test_python_written_config_is_canonical() -> None:
    # Swift's ConfigStore must write byte-identical JSON (ADR-0004); the Swift test compares
    # its serializer against this file, so it must be exactly what `dumps_json` produces.
    text = (FIXTURES / "config" / "python_written.json").read_text(encoding="utf-8")
    assert dumps_json(json.loads(text)) == text


def test_invalid_config_fixture_matches_validator_report() -> None:
    raw = json.loads((FIXTURES / "config" / "invalid.json").read_text(encoding="utf-8"))
    report_file = FIXTURES / "ccs" / "config_validate_invalid.json"
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert [i.to_dict() for i in validate(raw)] == report["issues"]


def test_statusline_preview_fixture_shape() -> None:
    doc = json.loads((FIXTURES / "ccs" / "statusline_preview.json").read_text(encoding="utf-8"))
    assert doc["ok"] is True
    assert {"applied", "script_path", "script_current"} <= set(doc["status"])
    colors = {seg["color"] for sample in doc["samples"] for seg in sample["segments"]}
    assert colors <= {"green", "yellow", "red", "gray", "plain"}
