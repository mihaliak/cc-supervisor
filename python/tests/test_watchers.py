"""Live-report and config watchers."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import (
    make_daemon,
    run_with_daemon,
    scenario,
    short_state_dir,
    wait_until,
    write_config,
)

from ccs import paths
from ccs.config import store
from ccs.config.models import Config
from ccs.daemon.server import Daemon
from ccs.fsio import atomic_write_json
from ccs.usage.model import UsageSnapshot, Window, format_iso


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        yield d


def live(now: datetime, percent: int, *, wrapper: str | None = "w1", **kw: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "profile_id": "work",
        "wrapper_id": wrapper,
        "session_id": kw.get("session_id", "s-1"),
        "model_id": kw.get("model_id", "claude-fable-5-1"),
        "effort": "high",
        "cwd": "/tmp",
        "observed_at": format_iso(now),
        "rate_limits": {
            "five_hour": {"percent": percent, "resets_at": format_iso(now + timedelta(hours=2))}
        },
    }


def test_live_report_updates_merged_snapshot(env: Path) -> None:
    async def scan_scenario() -> None:
        daemon = make_daemon()
        daemon.config = store.load()[0]
        now = daemon.clock.now().replace(microsecond=0)
        old = now - timedelta(minutes=2)
        daemon.polled["work"] = UsageSnapshot(
            profile_id="work",
            status="ok",
            fetched_at=old,
            polled_at=old,
            session=Window(40, now + timedelta(hours=2), old),
        )
        daemon.sessions["w1"] = {"wrapper_id": "w1", "profile_id": "work", "session_id": None}
        hits: list[str] = []
        daemon.hooks.on_usage_updated(hits.append)
        atomic_write_json(paths.live_file(wrapper_id="w1"), live(now, 77))
        affected = await daemon.live_watcher.scan_once()
        assert affected == {"work"}
        merged = daemon.snapshots["work"]
        assert merged.session is not None and merged.session.percent == 77
        assert merged.session.source == "statusline"
        on_disk = json.loads(paths.usage_file("work").read_text())
        assert on_disk["windows"]["session"]["percent"] == 77
        assert hits == ["work"]
        rec = json.loads(paths.session_file("w1").read_text())
        assert rec["model_id"] == "claude-fable-5-1" and rec["session_id"] == "s-1"
        # unchanged file → nothing re-merged
        assert await daemon.live_watcher.scan_once() == set()
        # removed file → falls back to the polled value
        paths.live_file(wrapper_id="w1").unlink()
        assert await daemon.live_watcher.scan_once() == {"work"}
        assert daemon.snapshots["work"].session.percent == 40  # type: ignore[union-attr]

    paths.ensure_state_layout()
    asyncio.run(scan_scenario())


def test_old_live_files_are_garbage_collected(env: Path) -> None:
    async def scan() -> None:
        daemon = make_daemon()
        daemon.config = store.load()[0]
        p = paths.live_file(session_id="old")
        atomic_write_json(p, live(datetime.now(UTC), 10, wrapper=None))
        ancient = (datetime.now(UTC) - timedelta(days=2)).timestamp()
        os.utime(p, (ancient, ancient))
        await daemon.live_watcher.scan_once()
        assert not p.exists()

    paths.ensure_state_layout()
    asyncio.run(scan())


def test_invalid_config_keeps_last_good_and_emits(env: Path) -> None:
    async def body(daemon: Daemon) -> None:
        good = daemon.config
        assert good is not None
        raw = json.loads(paths.config_file().read_text())
        raw["revision"] = 7
        raw["profiles"][0]["limits"] = {"session": {"warn": 95, "pause": 90}}
        paths.config_file().write_text(json.dumps(raw))
        await wait_until(
            lambda: (
                paths.events_file().exists() and "config.invalid" in paths.events_file().read_text()
            )
        )
        assert daemon.config is good
        events = [json.loads(x) for x in paths.events_file().read_text().splitlines()]
        inv = next(e for e in events if e["type"] == "config.invalid")
        assert inv["key"] == "config_invalid:7"
        assert inv["data"]["issues"] and inv["data"]["notify"] is True

    run_with_daemon(body)


def test_valid_config_change_fires_hook_and_restarts_polls(env: Path, tmp_path: Path) -> None:
    changes: list[tuple[int | None, int]] = []

    def install(daemon: Daemon) -> None:
        def changed(old: Config | None, new: Config) -> None:
            changes.append((old.revision if old else None, new.revision))

        daemon.hooks.on_config_changed(changed)

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: daemon.snapshots.get("work") is not None)
        new_dir = tmp_path / "profile-home"
        new_dir.mkdir()

        def add(raw: dict[str, Any]) -> None:
            raw["profiles"].append(
                {
                    "id": "home",
                    "flag": "home",
                    "name": "Home",
                    "emoji": "🏠",
                    "config_dir": str(new_dir),
                }
            )

        store.save(add)
        await wait_until(lambda: daemon.profile("home") is not None)
        await wait_until(lambda: daemon.snapshots.get("home") is not None)
        store.save(
            lambda raw: raw.update(
                profiles=[p for p in raw["profiles"] if p["id"] == "home"], default_profile="home"
            )
        )
        await wait_until(lambda: daemon.profile("work") is None)

    run_with_daemon(body, extensions=[install])
    assert changes[0] == (None, 0)
    assert changes[1:] == [(0, 1), (1, 2)]
