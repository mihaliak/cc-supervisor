"""Holds survive a daemon restart; the reconnecting launcher resumes exactly once (P06)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from daemon_helpers import run_with_daemon, scenario, short_state_dir, wait_until, write_config
from supervisor_helpers import FakeLauncher, set_usage, usage_payload

from ccs import paths
from ccs.clock import FakeClock
from ccs.daemon.server import Daemon
from ccs.supervisor import engine as engine_mod
from ccs.supervisor.model import ProfileSupervisorState

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=3)
PROMPT = "The usage limit window has reset. Continue exactly where you left off."


def test_hold_survives_restart_and_resumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engines: list[engine_mod.SupervisorEngine] = []

    def install(daemon: Daemon) -> None:
        engines.append(engine_mod.install(daemon))

    with short_state_dir(monkeypatch):
        scenario(tmp_path, monkeypatch, {})
        scen = tmp_path / "fake-scenario.json"
        set_usage(scen, usage_payload((95, RESET)))
        write_config(tmp_path)

        async def first(daemon: Daemon) -> None:
            w1 = FakeLauncher("w1", busy=True)
            await w1.start()
            await wait_until(lambda: w1.types() == ["pause"])
            await engines[-1].idle()
            await wait_until(
                lambda: daemon.sessions["w1"]["supervision"].get("was_busy_at_pause") is True
            )
            await w1.close()  # launcher disconnects; its record is kept

        run_with_daemon(first, clock=FakeClock(NOW), extensions=[install])
        saved = ProfileSupervisorState.from_dict(
            json.loads(paths.supervisor_file("work").read_text())
        )
        assert [h.id for h in saved.holds] == ["session"]

        clock = FakeClock(NOW + timedelta(minutes=5))

        async def second(daemon: Daemon) -> None:
            eng = engines[-1]
            assert [h.id for h in eng.state("work").holds] == ["session"]
            assert daemon.sessions["w1"]["supervision"]["state"] == "paused"
            w1 = FakeLauncher("w1", busy=False)
            reply = await w1.start()  # reconnect with the same wrapper id
            assert reply["supervision"]["state"] == "paused"
            await daemon.request_poll("work")
            await eng.idle()
            assert w1.types() == []  # no duplicate pause after the restart
            set_usage(scen, usage_payload((2, RESET + timedelta(hours=5))))
            clock.set(RESET + timedelta(seconds=16))
            await wait_until(lambda: w1.types() == ["resume"], timeout=10)
            await eng.idle()
            await daemon.request_poll("work")
            await eng.idle()
            assert w1.types() == ["resume"]
            assert w1.cmds[0]["prompt"] == PROMPT
            assert not eng.state("work").holds
            await w1.close()

        run_with_daemon(second, clock=clock, extensions=[install])
        final = json.loads(paths.supervisor_file("work").read_text())
        assert final["holds"] == []
        assert any(e["action"] == "resume" for e in final["ledger"])
