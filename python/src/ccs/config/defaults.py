"""The single source of every config default (ADR-0004). Nothing else hardcodes these."""

from __future__ import annotations

import copy
from typing import Any

CONFIG_VERSION = 1

RESUME_PROMPT = "The usage limit window has reset. Continue exactly where you left off."
WARMUP_PROMPT = "Reply with just: ok"
WARMUP_MODEL = "haiku"

_DISPLAY: dict[str, Any] = {
    "time_format": "24h",
    "colors": {"yellow_from": 50, "red_from": 80},
    "menu_bar": "emoji_percent",
}

_POLLING: dict[str, Any] = {
    "interval_seconds": 60,
    "fast_interval_seconds": 20,
    "fast_when_percent_at_least": 70,
    "idle_interval_seconds": 120,
}

_NOTIFICATIONS: dict[str, Any] = {
    "limit_warn": True,
    "limit_pause": True,
    "limit_resume": True,
    "warmup": True,
    "errors": True,
}

_LIMITS: dict[str, Any] = {
    "session": {"warn": 80, "pause": 90},
    "weekly": {"warn": 80, "pause": 95},
    "model_scoped": {"warn": 80, "pause": 95, "warn_only": False},
    "extra_usage": {"spill": False, "warn": 80, "pause": 90},
}

_SUPERVISOR: dict[str, Any] = {"enabled": True, "resume_prompt": RESUME_PROMPT}

_STATUSLINE: dict[str, Any] = {"enabled": True}

_WARMUP: dict[str, Any] = {
    "enabled": True,
    "model": WARMUP_MODEL,
    "prompt": WARMUP_PROMPT,
    "triggers": {
        "schedule": [],
        "app_start": True,
        "unlock_wake": True,
        "auto_chain": True,
    },
    "active_hours": {"start": "07:00", "end": "23:00"},
    "cooldown_minutes": 10,
}


def default_config_dict() -> dict[str, Any]:
    """Top-level defaults (no profiles). A fresh deep copy on every call."""
    return {
        "version": CONFIG_VERSION,
        "revision": 0,
        "default_profile": "personal",
        "ccs_path": None,
        "claude_path": None,
        "display": copy.deepcopy(_DISPLAY),
        "polling": copy.deepcopy(_POLLING),
        "notifications": copy.deepcopy(_NOTIFICATIONS),
        "profiles": [],
    }


def profile_defaults() -> dict[str, Any]:
    """Defaults for the optional parts of a profile (everything but identity)."""
    return {
        "limits": copy.deepcopy(_LIMITS),
        "supervisor": copy.deepcopy(_SUPERVISOR),
        "statusline": copy.deepcopy(_STATUSLINE),
        "warmup": copy.deepcopy(_WARMUP),
    }


def default_profile_dict(
    id: str, flag: str, name: str, emoji: str, config_dir: str
) -> dict[str, Any]:
    """A complete profile dict with the given identity and default everything else."""
    return {
        "id": id,
        "flag": flag,
        "name": name,
        "emoji": emoji,
        "config_dir": config_dir,
        **profile_defaults(),
    }
