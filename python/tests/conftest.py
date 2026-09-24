from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FAKE_CLAUDE = Path(__file__).parent / "fake_claude" / "claude"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("CCS_TEST_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test: set CCS_TEST_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@dataclass(frozen=True)
class XdgDirs:
    config_home: Path
    state_home: Path


@pytest.fixture
def tmp_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> XdgDirs:
    """Point XDG config/state homes at a temp dir so tests never touch real files."""
    dirs = XdgDirs(config_home=tmp_path / "xdg-config", state_home=tmp_path / "xdg-state")
    dirs.config_home.mkdir()
    dirs.state_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(dirs.config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(dirs.state_home))
    monkeypatch.delenv("CCS_STATE_DIR", raising=False)
    return dirs


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake HOME so `~` never resolves into the real home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_claude_path() -> Path:
    return FAKE_CLAUDE


@dataclass(frozen=True)
class FakeClaude:
    path: Path
    env: dict[str, str]
    log_path: Path

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[dict[str, Any]], FakeClaude]]:
    """Factory: write a scenario, set FAKE_CLAUDE_* env vars, return the fake's handle."""

    def make(scenario: dict[str, Any]) -> FakeClaude:
        scenario_path = tmp_path / "fake-claude-scenario.json"
        log_path = tmp_path / "fake-claude-calls.jsonl"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(scenario_path))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
        env = dict(os.environ)
        return FakeClaude(path=FAKE_CLAUDE, env=env, log_path=log_path)

    yield make
