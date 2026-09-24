"""`claude agents --json` sampling."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from daemon_helpers import make_daemon, scenario, short_state_dir, write_config

from ccs import paths
from ccs.config import store
from ccs.daemon.sampler import classify_agents

ENTRIES = [
    {"pid": 100, "kind": "interactive", "status": "busy", "sessionId": "sa"},
    {"pid": 200, "kind": "interactive", "status": "idle", "sessionId": "sb"},
    {"pid": 300, "kind": "interactive", "status": "idle", "sessionId": "probe"},
    {"id": "abc", "kind": "background", "state": "blocked", "sessionId": "sc"},
    {"pid": 400, "kind": "daemon-worker", "status": "busy"},
    {"pid": 500, "kind": "interactive", "status": "weird", "sessionId": "sd"},
]


def test_classify_maps_wrappers_and_counts_others() -> None:
    sample = classify_agents(ENTRIES, {100: "w1", 500: "w5"}, own_pids={300})
    assert sample.activity == {"w1": ("busy", "sa"), "w5": ("unknown", "sd")}
    assert sample.counts == {"interactive": 1, "background": 2}


def test_classify_empty() -> None:
    sample = classify_agents([], {}, set())
    assert sample.activity == {} and sample.counts == {"interactive": 0, "background": 0}


def test_sample_profile_updates_session_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with short_state_dir(monkeypatch):
        scenario(
            tmp_path,
            monkeypatch,
            {
                "agents": {
                    "sessions": [
                        {
                            "pid": "$PID0",
                            "kind": "interactive",
                            "status": "busy",
                            "sessionId": "s1",
                        },
                        {
                            "pid": "$PID1",
                            "kind": "interactive",
                            "status": "idle",
                            "sessionId": "s2",
                        },
                        {"kind": "background", "state": "blocked", "sessionId": "s3"},
                    ]
                }
            },
        )
        monkeypatch.setenv("FAKE_CLAUDE_AGENTS_PIDS", f"{os.getpid()},999999")
        write_config(tmp_path)
        paths.ensure_state_layout()

        async def run() -> None:
            daemon = make_daemon()
            daemon.config = store.load()[0]
            daemon.sessions["w1"] = {
                "wrapper_id": "w1",
                "profile_id": "work",
                "claude_pid": os.getpid(),
                "activity": "unknown",
                "session_id": None,
            }
            sample = await daemon.sampler.sample_profile("work")
            assert sample is not None
            assert daemon.sessions["w1"]["activity"] == "busy"
            assert daemon.sessions["w1"]["session_id"] == "s1"
            assert daemon.other_sessions["work"] == {"interactive": 1, "background": 1}
            rec = json.loads(paths.session_file("w1").read_text())
            assert rec["activity"] == "busy"

        asyncio.run(run())
