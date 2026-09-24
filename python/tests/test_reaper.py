"""Reaper: dead launcher pids are unregistered with reason `reaped`."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from daemon_helpers import make_daemon, run_with_daemon, scenario, short_state_dir, write_config

from ccs import paths
from ccs.config import store
from ccs.daemon.reaper import pid_alive
from ccs.daemon.server import Daemon, new_session_record
from ccs.fsio import atomic_write_json


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid())
    assert not pid_alive(0)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        paths.ensure_state_layout()
        yield d


def events() -> list[dict[str, object]]:
    return [json.loads(x) for x in paths.events_file().read_text().splitlines()]


def test_reap_once_removes_dead_wrapper(env: Path) -> None:
    async def run() -> None:
        daemon = make_daemon()
        daemon.config = store.load()[0]
        now = daemon.clock.now()
        alive = new_session_record(
            wrapper_id="alive",
            profile_id="work",
            wrapper_pid=os.getpid(),
            claude_pid=None,
            cwd=None,
            now=now,
        )
        dead = new_session_record(
            wrapper_id="dead",
            profile_id="work",
            wrapper_pid=dead_pid(),
            claude_pid=None,
            cwd=None,
            now=now,
        )
        for rec in (alive, dead):
            daemon.sessions[str(rec["wrapper_id"])] = rec
            daemon.write_session(str(rec["wrapper_id"]))
        assert await daemon.reaper.reap_once() == ["dead"]
        assert set(daemon.sessions) == {"alive"}
        assert not paths.session_file("dead").exists()
        assert paths.session_file("alive").exists()

    asyncio.run(run())
    ended = [e for e in events() if e["type"] == "session.ended"]
    assert len(ended) == 1
    assert ended[0]["data"]["reason"] == "reaped"  # type: ignore[index]


def test_startup_drops_dead_records(env: Path) -> None:
    now_rec = {
        "schema": 1,
        "wrapper_id": "gone",
        "profile_id": "work",
        "wrapper_pid": dead_pid(),
    }
    atomic_write_json(paths.session_file("gone"), now_rec)
    live_rec = {
        "schema": 1,
        "wrapper_id": "here",
        "profile_id": "work",
        "wrapper_pid": os.getpid(),
    }
    atomic_write_json(paths.session_file("here"), live_rec)
    atomic_write_json(paths.session_file("junk"), {"nope": True})

    async def body(daemon: Daemon) -> None:
        assert set(daemon.sessions) == {"here"}

    run_with_daemon(body)
    assert not paths.session_file("gone").exists()
    assert not paths.session_file("junk").exists()
    assert paths.session_file("here").exists()
