from __future__ import annotations

from pathlib import Path

from ccs.config.seed import seed_config
from ccs.config.validate import validate


def ids(cfg: dict[str, object]) -> list[str]:
    profiles = cfg["profiles"]
    assert isinstance(profiles, list)
    return [p["id"] for p in profiles]


def test_seed_both(tmp_home: Path) -> None:
    (tmp_home / ".claude").mkdir()
    (tmp_home / ".claude-work").mkdir()
    cfg = seed_config()
    assert ids(cfg) == ["personal", "work"]
    assert cfg["default_profile"] == "personal"
    assert validate(cfg) == []
    profiles = cfg["profiles"]
    assert isinstance(profiles, list)
    assert profiles[0]["emoji"] == "🏠" and profiles[1]["emoji"] == "💼"
    assert profiles[1]["config_dir"] == "~/.claude-work"


def test_seed_only_work(tmp_home: Path) -> None:
    (tmp_home / ".claude-work").mkdir()
    cfg = seed_config()
    assert ids(cfg) == ["work"]
    assert cfg["default_profile"] == "work"


def test_seed_none_exist(tmp_home: Path) -> None:
    cfg = seed_config()
    assert ids(cfg) == ["personal"]
    assert cfg["default_profile"] == "personal"
    assert validate(cfg) == []
