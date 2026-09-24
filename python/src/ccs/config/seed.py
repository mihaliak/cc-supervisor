"""First-run seed: `personal` (~/.claude) and `work` (~/.claude-work) (ADR-0004)."""

from __future__ import annotations

import os
from typing import Any

from ccs.config.defaults import default_config_dict, default_profile_dict

SEED_PROFILES: tuple[tuple[str, str, str, str], ...] = (
    # id, name, emoji, config_dir
    ("personal", "Personal", "🏠", "~/.claude"),
    ("work", "Work", "💼", "~/.claude-work"),
)


def seed_config() -> dict[str, Any]:
    """Defaults plus the seed profiles whose config dir exists (`personal` if none does)."""
    cfg = default_config_dict()
    for pid, name, emoji, cdir in SEED_PROFILES:
        if os.path.isdir(os.path.expanduser(cdir)):
            cfg["profiles"].append(default_profile_dict(pid, pid, name, emoji, cdir))
    if not cfg["profiles"]:
        pid, name, emoji, cdir = SEED_PROFILES[0]
        cfg["profiles"].append(default_profile_dict(pid, pid, name, emoji, cdir))
    cfg["default_profile"] = cfg["profiles"][0]["id"]
    return cfg
