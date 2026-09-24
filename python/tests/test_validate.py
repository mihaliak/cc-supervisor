from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from ccs.config.defaults import default_config_dict, default_profile_dict
from ccs.config.validate import CLAUDE_LONG_OPTIONS, RESERVED_FLAGS, validate


def base() -> dict[str, Any]:
    cfg = default_config_dict()
    cfg["profiles"] = [
        default_profile_dict("personal", "personal", "Personal", "🏠", "~/.claude"),
        default_profile_dict("work", "work", "Work", "💼", "~/.claude-work"),
    ]
    return cfg


def minimal() -> dict[str, Any]:
    return {
        "version": 1,
        "default_profile": "work",
        "profiles": [
            {
                "id": "work",
                "flag": "work",
                "name": "Work",
                "emoji": "💼",
                "config_dir": "~/.claude-work",
            }
        ],
    }


def test_defaults_and_minimal_are_valid() -> None:
    assert validate(base()) == []
    assert validate(minimal()) == []


def test_no_profiles_is_valid() -> None:
    cfg = default_config_dict()
    assert validate(cfg) == []


def _set(path: list[Any], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(cfg: dict[str, Any]) -> None:
        node: Any = cfg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _dup(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(cfg: dict[str, Any]) -> None:
        cfg["profiles"][1][key] = cfg["profiles"][0][key]

    return mutate


CASES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("dup id", _dup("id"), "profiles[1].id"),
    ("dup flag", _dup("flag"), "profiles[1].flag"),
    ("dup config_dir", _set(["profiles", 1, "config_dir"], "~/.claude"), "profiles[1].config_dir"),
    ("reserved resume", _set(["profiles", 0, "flag"], "resume"), "profiles[0].flag"),
    ("reserved help", _set(["profiles", 0, "flag"], "help"), "profiles[0].flag"),
    ("reserved no-supervise", _set(["profiles", 0, "flag"], "no-supervise"), "profiles[0].flag"),
    ("bad id", _set(["profiles", 0, "id"], "Work"), "profiles[0].id"),
    (
        "warn >= pause",
        _set(["profiles", 0, "limits", "session", "warn"], 90),
        "profiles[0].limits.session.warn",
    ),
    (
        "pause > 100",
        _set(["profiles", 0, "limits", "weekly", "pause"], 101),
        "profiles[0].limits.weekly.pause",
    ),
    (
        "bool threshold",
        _set(["profiles", 0, "limits", "weekly", "warn"], True),
        "profiles[0].limits.weekly.warn",
    ),
    (
        "warn_only type",
        _set(["profiles", 0, "limits", "model_scoped", "warn_only"], "no"),
        "profiles[0].limits.model_scoped.warn_only",
    ),
    (
        "bad time",
        _set(
            ["profiles", 0, "warmup", "triggers", "schedule"],
            [{"time": "6:00", "weekdays": ["mon"]}],
        ),
        "profiles[0].warmup.triggers.schedule[0].time",
    ),
    (
        "bad weekday",
        _set(
            ["profiles", 0, "warmup", "triggers", "schedule"],
            [{"time": "06:00", "weekdays": ["monday"]}],
        ),
        "profiles[0].warmup.triggers.schedule[0].weekdays",
    ),
    (
        "dup weekday",
        _set(
            ["profiles", 0, "warmup", "triggers", "schedule"],
            [{"time": "06:00", "weekdays": ["mon", "mon"]}],
        ),
        "profiles[0].warmup.triggers.schedule[0].weekdays",
    ),
    (
        "active hours",
        _set(["profiles", 0, "warmup", "active_hours", "end"], "24:00"),
        "profiles[0].warmup.active_hours.end",
    ),
    (
        "cooldown",
        _set(["profiles", 0, "warmup", "cooldown_minutes"], 2000),
        "profiles[0].warmup.cooldown_minutes",
    ),
    ("colors order", _set(["display", "colors", "yellow_from"], 80), "display.colors"),
    ("menu bar", _set(["display", "menu_bar"], "text"), "display.menu_bar"),
    ("time format", _set(["display", "time_format"], "12h"), "display.time_format"),
    ("polling", _set(["polling", "interval_seconds"], 2), "polling.interval_seconds"),
    ("notification type", _set(["notifications", "errors"], 1), "notifications.errors"),
    ("default missing", _set(["default_profile"], "nope"), "default_profile"),
    ("version newer", _set(["version"], 2), "version"),
    ("revision negative", _set(["revision"], -1), "revision"),
    ("emoji empty", _set(["profiles", 0, "emoji"], ""), "profiles[0].emoji"),
    ("emoji long", _set(["profiles", 0, "emoji"], "123456789"), "profiles[0].emoji"),
    (
        "relative config dir",
        _set(["profiles", 0, "config_dir"], "claude"),
        "profiles[0].config_dir",
    ),
    ("limits not object", _set(["profiles", 0, "limits"], "x"), "profiles[0].limits"),
    ("profiles not list", _set(["profiles"], {}), "profiles"),
    ("claude_path empty", _set(["claude_path"], ""), "claude_path"),
]


@pytest.mark.parametrize(("name", "mutate", "path"), CASES, ids=[c[0] for c in CASES])
def test_invalid(name: str, mutate: Callable[[dict[str, Any]], None], path: str) -> None:
    cfg = copy.deepcopy(base())
    mutate(cfg)
    issues = validate(cfg)
    assert path in [i.path for i in issues], issues


def test_non_object() -> None:
    assert [i.path for i in validate([])] == [""]


def test_version_newer_message() -> None:
    cfg = base()
    cfg["version"] = 2
    msgs = [i.message for i in validate(cfg)]
    assert any("newer" in m for m in msgs)


def test_reserved_flags_cover_ccs_and_claude() -> None:
    assert {"help", "version", "profile", "force", "no-supervise", "json"} <= RESERVED_FLAGS
    assert {"resume", "continue", "model", "print", "effort", "bg"} <= CLAUDE_LONG_OPTIONS
