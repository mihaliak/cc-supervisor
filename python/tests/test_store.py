from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import XdgDirs

from ccs import paths
from ccs.config import store


def write_minimal(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 0,
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
        ),
        encoding="utf-8",
    )


def test_load_missing(tmp_xdg: XdgDirs) -> None:
    with pytest.raises(store.ConfigMissing):
        store.load()


def test_load_invalid_json(tmp_xdg: XdgDirs) -> None:
    path = paths.config_file()
    path.parent.mkdir(parents=True)
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(store.ConfigInvalid):
        store.load()


def test_revision_increments(tmp_xdg: XdgDirs) -> None:
    write_minimal(paths.config_file())
    cfg = store.save(lambda raw: raw.update(default_profile="work"))
    assert cfg.revision == 1
    cfg = store.save(lambda raw: None, expected_revision=1)
    assert cfg.revision == 2


def test_revision_conflict(tmp_xdg: XdgDirs) -> None:
    write_minimal(paths.config_file())
    store.save(lambda raw: None)
    with pytest.raises(store.RevisionConflict) as err:
        store.save(lambda raw: None, expected_revision=0)
    assert err.value.actual == 1


def test_invalid_mutation_leaves_file(tmp_xdg: XdgDirs) -> None:
    path = paths.config_file()
    write_minimal(path)
    before = path.read_bytes()

    def bad(raw: dict[str, Any]) -> None:
        raw["profiles"][0]["limits"] = {"session": {"warn": 95, "pause": 90}}

    with pytest.raises(store.ConfigInvalid) as err:
        store.save(bad)
    assert "profiles[0].limits.session.warn" in [i.path for i in err.value.issues]
    assert path.read_bytes() == before


def test_ensure_config_seeds(tmp_xdg: XdgDirs, tmp_home: Path) -> None:
    (tmp_home / ".claude-work").mkdir()
    cfg = store.ensure_config()
    assert [p.id for p in cfg.profiles] == ["work"]
    assert cfg.default_profile == "work"
    assert paths.config_file().exists()
    # second call only loads
    assert store.ensure_config().revision == cfg.revision


def _bump(config_path: str, times: int) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["counter"] = int(raw.get("counter", 0)) + 1

    for _ in range(times):
        store.save(mutate, path=Path(config_path))


def test_concurrent_saves_no_lost_update(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "config.json"
    write_minimal(path)
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_bump, args=(str(path), 15)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["counter"] == 30
    assert raw["revision"] == 30
    leftovers = [n for n in os.listdir(path.parent) if n.endswith(".tmp")]
    assert leftovers == []
