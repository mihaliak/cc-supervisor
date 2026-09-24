from __future__ import annotations

import json
from typing import Any

from conftest import XdgDirs

from ccs import paths
from ccs.config import store
from ccs.config.models import Config, deep_merge


def raw_with_unknowns() -> dict[str, Any]:
    return {
        "version": 1,
        "revision": 3,
        "default_profile": "work",
        "future_top": {"x": 1},
        "display": {"colors": {"yellow_from": 40, "red_from": 70, "future_color": "teal"}},
        "profiles": [
            {
                "id": "work",
                "flag": "work",
                "name": "Work",
                "emoji": "💼",
                "config_dir": "~/.claude-work",
                "future_profile_key": [1, 2],
                "limits": {"session": {"warn": 70, "pause": 85, "future_nested": True}},
                "warmup": {
                    "triggers": {"schedule": [{"time": "06:00", "weekdays": ["mon"], "note": "n"}]}
                },
            }
        ],
    }


def test_from_dict_fills_defaults_and_keeps_extra() -> None:
    cfg = Config.from_dict(raw_with_unknowns())
    prof = cfg.profiles[0]
    assert cfg.display.colors.yellow_from == 40
    assert cfg.display.colors.extra == {"future_color": "teal"}
    assert cfg.polling.interval_seconds == 60
    assert prof.limits.session.warn == 70 and prof.limits.session.extra == {"future_nested": True}
    assert prof.limits.weekly.pause == 95
    assert prof.warmup.model == "haiku"
    assert prof.warmup.triggers.schedule[0].extra == {"note": "n"}
    assert cfg.extra == {"future_top": {"x": 1}}
    assert prof.extra == {"future_profile_key": [1, 2]}


def contains(outer: Any, inner: Any) -> bool:
    """Every key/value of `inner` is present in `outer` (lists compared element-wise)."""
    if isinstance(inner, dict):
        return isinstance(outer, dict) and all(
            k in outer and contains(outer[k], v) for k, v in inner.items()
        )
    if isinstance(inner, list):
        return (
            isinstance(outer, list)
            and len(outer) == len(inner)
            and all(contains(o, i) for o, i in zip(outer, inner, strict=True))
        )
    return bool(outer == inner)


def test_to_dict_reemits_everything() -> None:
    raw = raw_with_unknowns()
    out = Config.from_dict(raw).to_dict()
    # every raw key survives with its value (defaults added around it)
    assert contains(out, raw)
    assert deep_merge(out, {"future_top": {"x": 1}}) == out
    assert out["future_top"] == {"x": 1}
    assert out["profiles"][0]["future_profile_key"] == [1, 2]


def test_colors_level() -> None:
    colors = Config.from_dict({"version": 1, "profiles": []}).display.colors
    assert [colors.level(p) for p in (0, 49, 50, 79, 80, 100)] == [
        "green",
        "green",
        "yellow",
        "yellow",
        "red",
        "red",
    ]


def test_unknown_keys_survive_save(tmp_xdg: XdgDirs) -> None:
    path = paths.config_file()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw_with_unknowns()), encoding="utf-8")

    def mutate(raw: dict[str, Any]) -> None:
        raw["profiles"][0]["name"] = "Work 2"

    store.save(mutate)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["future_top"] == {"x": 1}
    assert saved["display"]["colors"]["future_color"] == "teal"
    assert saved["profiles"][0]["future_profile_key"] == [1, 2]
    assert saved["profiles"][0]["limits"]["session"]["future_nested"] is True
    assert saved["profiles"][0]["name"] == "Work 2"
    assert saved["revision"] == 4
    # minimal file stays minimal: save does not inject defaults
    assert "polling" not in saved
