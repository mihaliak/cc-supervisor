from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from conftest import XdgDirs

from ccs import paths


def test_xdg_overrides(tmp_xdg: XdgDirs) -> None:
    assert paths.config_dir() == tmp_xdg.config_home / "ccs"
    assert paths.config_file() == tmp_xdg.config_home / "ccs" / "config.json"
    assert paths.state_dir() == tmp_xdg.state_home / "ccs"


def test_defaults_without_xdg(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("CCS_STATE_DIR", raising=False)
    assert paths.config_dir() == tmp_home / ".config" / "ccs"
    assert paths.state_dir() == tmp_home / ".local" / "state" / "ccs"


def test_relative_xdg_is_ignored(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert paths.config_dir() == tmp_home / ".config" / "ccs"


def test_ccs_state_dir_override(
    tmp_xdg: XdgDirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-state"
    monkeypatch.setenv("CCS_STATE_DIR", str(override))
    assert paths.state_dir() == override
    assert paths.usage_file("work") == override / "usage" / "work.json"


def test_state_helpers(tmp_xdg: XdgDirs) -> None:
    root = tmp_xdg.state_home / "ccs"
    assert paths.daemon_sock() == root / "daemon.sock"
    assert paths.daemon_lock() == root / "daemon.lock"
    assert paths.daemon_log() == root / "logs" / "daemon.log"
    assert paths.session_file("w1") == root / "sessions" / "w1.json"
    assert paths.supervisor_file("work") == root / "supervisor" / "work.json"
    assert paths.warmup_file("work") == root / "warmup" / "work.json"
    assert paths.warmup_cwd() == root / "warmup" / "cwd"
    assert paths.statusline_file("work") == root / "statusline" / "work.json"
    assert paths.widget_snapshot() == root / "widget" / "snapshot.json"
    assert paths.events_file() == root / "events.jsonl"
    assert paths.events_seen_file() == root / "events.seen.json"
    assert paths.live_file(wrapper_id="abc") == root / "live" / "abc.json"
    assert paths.live_file(session_id="s1") == root / "live" / "session-s1.json"
    with pytest.raises(ValueError):
        paths.live_file()


def test_ensure_state_layout(tmp_xdg: XdgDirs) -> None:
    root = paths.ensure_state_layout()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    for sub in (
        "logs",
        "usage",
        "live",
        "sessions",
        "supervisor",
        "warmup/cwd",
        "statusline",
        "widget",
    ):
        assert (root / sub).is_dir()


def test_expand_config_dir(tmp_home: Path) -> None:
    target = tmp_home / "real-claude"
    target.mkdir()
    (tmp_home / ".claude").symlink_to(target)
    assert paths.expand_config_dir("~/.claude") == target.resolve()
    assert paths.config_dir_env_value("~/.claude") == str(tmp_home / ".claude")
