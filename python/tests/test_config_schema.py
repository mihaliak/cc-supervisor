"""Structural check of schema/config.schema.json against the Python defaults (stdlib only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ccs.config.defaults import default_config_dict, default_profile_dict
from ccs.config.seed import seed_config

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "config.schema.json").read_text("utf-8")
)


def keys_match(value: dict[str, Any], schema: dict[str, Any], where: str) -> None:
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        assert key in value, f"{where}: required key {key} missing"
    for key, sub in value.items():
        assert key in props, f"{where}: {key} not in schema"
        if isinstance(sub, dict) and props[key].get("type") == "object":
            keys_match(sub, props[key], f"{where}.{key}")


def test_defaults_match_schema() -> None:
    cfg = default_config_dict()
    keys_match(cfg, SCHEMA, "config")
    profile_schema = SCHEMA["$defs"]["profile"]
    keys_match(default_profile_dict("p", "p", "P", "🙂", "~/.p"), profile_schema, "profile")


def test_seed_matches_schema(tmp_home: Path) -> None:
    cfg = seed_config()
    keys_match(cfg, SCHEMA, "seed")
    for prof in cfg["profiles"]:
        keys_match(prof, SCHEMA["$defs"]["profile"], "seed.profile")
