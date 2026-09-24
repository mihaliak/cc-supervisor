"""Supervisor engine end to end: in-process daemon, fake claude, fake launcher clients (P06)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import run_with_daemon, scenario, short_state_dir, wait_until, write_config
from schema_check import validate
from supervisor_helpers import FakeLauncher, set_usage, usage_payload

from ccs import paths
from ccs.clock import FakeClock
from ccs.daemon.client import AsyncDaemonClient
from ccs.daemon.server import Daemon
from ccs.events import read_tail
from ccs.supervisor import engine as engine_mod
from ccs.usage.model import format_iso

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=3)
WEEK = NOW + timedelta(days=2)
PROMPT = "The usage limit window has reset. Continue exactly where you left off."


@pytest.fixture
def scen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_state_dir(monkeypatch):
        scenario(tmp_path, monkeypatch, {})
        path = tmp_path / "fake-scenario.json"
        set_usage(path, usage_payload((50, RESET), (10, WEEK)))
        write_config(tmp_path)
        yield path


class Harness:
    """Collects the engine instance the daemon installs."""

    def __init__(self) -> None:
        self.engine: engine_mod.SupervisorEngine | None = None
        self.clock = FakeClock(NOW)

    def install(self, daemon: Daemon) -> None:
        self.engine = engine_mod.install(daemon)

    @property
    def eng(self) -> engine_mod.SupervisorEngine:
        assert self.engine is not None
        return self.engine

    def run(self, body: Any) -> Any:
        return run_with_daemon(body, clock=self.clock, extensions=[self.install])


def sup(daemon: Daemon, wid: str) -> dict[str, Any]:
    rec = daemon.sessions[wid]["supervision"]
    assert isinstance(rec, dict)
    return rec


def events_of(etype: str) -> list[dict[str, Any]]:
    return [e for e in read_tail(limit=1000) if e.get("type") == etype]


async def cli() -> AsyncDaemonClient:
    c = AsyncDaemonClient()
    await c.connect()
    await c.hello("cli")
    return c


def test_pause_then_reset_resumes_with_prompt_only_for_busy(scen: Path) -> None:
    h = Harness()

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        w1, w2 = FakeLauncher("w1", busy=True), FakeLauncher("w2", busy=False)
        await w1.start()
        await w2.start()
        set_usage(scen, usage_payload((95, RESET), (10, WEEK)))
        await daemon.request_poll("work")
        await wait_until(lambda: w1.types() == ["pause"] and w2.types() == ["pause"])
        await wait_until(lambda: sup(daemon, "w2").get("was_busy_at_pause") is False)
        await wait_until(lambda: sup(daemon, "w1").get("was_busy_at_pause") is True)
        s1 = sup(daemon, "w1")
        assert s1["state"] == "paused" and s1["holds"] == ["session"]
        assert s1["resume_at"] == format_iso(RESET)
        assert w1.cmds[0]["holds"] == ["session"] and w1.cmds[0]["resume_at"] == format_iso(RESET)
        disk = json.loads(paths.session_file("w1").read_text())
        assert disk["supervision"]["state"] == "paused" and disk["session_id"] == "s-w1"
        state = json.loads(paths.supervisor_file("work").read_text())
        assert [x["id"] for x in state["holds"]] == ["session"]
        assert validate(state, "supervisor-state.schema.json") == []
        assert validate(disk, "session-record.schema.json") == []
        (pause_ev,) = events_of("limit.pause")
        assert pause_ev["data"]["sessions_paused"] == 2
        assert pause_ev["data"]["title"] == "💼 Work paused at 95%"
        # status / snapshot contributions
        contrib = daemon.supervisor_state("work")
        assert contrib["state"] == "paused" and contrib["resume_at"] == format_iso(RESET)
        assert contrib["holds"][0]["id"] == "session"

        # the window resets: tick → confirmation poll → resume
        set_usage(scen, usage_payload((3, RESET + timedelta(hours=5)), (10, WEEK)))
        h.clock.set(RESET + timedelta(seconds=20))
        await wait_until(lambda: "resume" in w1.types() and "resume" in w2.types(), timeout=10)
        await h.eng.idle()
        r1 = next(c for c in w1.cmds if c["type"] == "resume")
        r2 = next(c for c in w2.cmds if c["type"] == "resume")
        assert r1["prompt"] == PROMPT and r2["prompt"] is None
        assert sup(daemon, "w1")["state"] == "running" and sup(daemon, "w1")["holds"] == []
        assert not h.eng.state("work").holds
        (resume_ev,) = events_of("limit.resume")
        assert resume_ev["data"]["sessions_resumed"] == 2
        assert resume_ev["data"]["reason"] == "reset_confirmed"
        # the same (old) instance never re-pauses; nothing else is sent
        await daemon.request_poll("work")
        await h.eng.idle()
        assert w1.types() == ["pause", "resume"] and w2.types() == ["pause", "resume"]
        await w1.close()
        await w2.close()

    h.run(body)


def test_override_gets_no_resume_prompt(scen: Path) -> None:
    h = Harness()

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        w1, w2 = FakeLauncher("w1"), FakeLauncher("w2")
        await w1.start()
        await w2.start()
        set_usage(scen, usage_payload((92, RESET)))
        await daemon.request_poll("work")
        await wait_until(lambda: w1.types() == ["pause"] and w2.types() == ["pause"])
        await h.eng.idle()
        reply = await w2.event("input_submitted_while_paused")
        assert reply["ok"] is True
        s2 = sup(daemon, "w2")
        assert s2["state"] == "overridden" and s2["overridden_instances"]
        assert events_of("limit.override")[0]["data"]["wrapper_id"] == "w2"
        # still over the threshold: the override holds, no new pause for w2
        await daemon.request_poll("work")
        await h.eng.idle()
        assert w2.types() == ["pause"]
        # reset: w1 resumes with the prompt, w2 just becomes running (no command)
        set_usage(scen, usage_payload((1, RESET + timedelta(hours=5))))
        h.clock.set(RESET + timedelta(seconds=30))
        await wait_until(lambda: "resume" in w1.types(), timeout=10)
        await wait_until(lambda: sup(daemon, "w2")["state"] == "running", timeout=10)
        await h.eng.idle()
        assert w2.types() == ["pause"]
        assert sup(daemon, "w2")["overridden_instances"] == []
        ledger = [e["action"] for e in h.eng.state("work").to_dict("work")["ledger"]]
        assert "override" in ledger
        await w1.close()
        await w2.close()

    h.run(body)


def test_manual_pause_and_resume_ops(scen: Path) -> None:
    h = Harness()

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        w1, w2 = FakeLauncher("w1", busy=True), FakeLauncher("w2", busy=True)
        await w1.start()
        await w2.start()
        c = await cli()
        assert (await c.request("pause", profile_id="nope"))["error"] == "unknown_profile"
        assert (await c.request("pause", wrapper_id="zz"))["error"] == "unknown_session"
        assert (await c.request("pause"))["error"] == "bad_request"
        reply = await c.request("pause", profile_id="work")
        assert reply["ok"] and reply["created"] and reply["sessions_paused"] == 2
        assert reply["hold"]["id"] == "manual" and reply["hold"]["scope"] is None
        again = await c.request("pause", profile_id="work")
        assert again["ok"] and not again["created"]
        await wait_until(lambda: w1.types() == ["pause"] and w2.types() == ["pause"])
        await h.eng.idle()
        assert sup(daemon, "w1")["holds"] == ["manual"] and sup(daemon, "w1")["resume_at"] is None
        assert events_of("limit.pause")[0]["data"]["title"] == "💼 Work paused manually"
        # a reset does not clear a manual hold
        h.clock.set(NOW + timedelta(hours=6))
        await daemon.request_poll("work")
        await h.eng.idle()
        assert h.eng.state("work").holds
        # resume one session: it becomes overridden for the manual instance, gets the prompt
        one = await c.request("resume", wrapper_id="w1")
        assert one["ok"] and one["sessions_resumed"] == 1
        await wait_until(lambda: "resume" in w1.types())
        assert sup(daemon, "w1")["state"] == "overridden"
        assert w1.cmds[-1]["prompt"] == PROMPT
        # resume the profile: w2 resumes with the prompt; w1 is not paused again
        allr = await c.request("resume", profile_id="work")
        assert allr["ok"] and allr["sessions_resumed"] == 1
        assert [x["id"] for x in allr["cleared"]] == ["manual"]
        await wait_until(lambda: "resume" in w2.types())
        await h.eng.idle()
        assert w2.cmds[-1]["prompt"] == PROMPT
        assert not h.eng.state("work").holds
        await wait_until(lambda: sup(daemon, "w1")["state"] == "running")
        manual_resumes = [e for e in events_of("limit.resume") if e["data"].get("manual")]
        assert len(manual_resumes) == 2
        # session-scoped manual pause only pauses that session
        scoped = await c.request("pause", wrapper_id="w2")
        assert scoped["ok"] and scoped["sessions_paused"] == 1
        await wait_until(lambda: w2.types().count("pause") == 2)
        await h.eng.idle()
        assert w1.types().count("pause") == 1
        # the launcher's start-while-held check ignores a session-scoped manual hold
        st = await c.request("status", profile_id="work")
        holds = st["profiles"][0]["supervisor"]["holds"]
        assert holds[0]["scope"] == "w2"
        await c.close()
        await w1.close()
        await w2.close()

    h.run(body)


def test_register_while_held(scen: Path) -> None:
    h = Harness()

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        set_usage(scen, usage_payload((96, RESET)))
        await daemon.request_poll("work")
        await wait_until(lambda: bool(h.eng.state("work").holds))
        over = FakeLauncher("w-over")
        reply = await over.start(started_overridden=True)
        assert reply["supervision"]["state"] == "overridden"
        normal = FakeLauncher("w-new")
        reply2 = await normal.start()
        assert reply2["supervision"]["state"] == "paused"
        await wait_until(lambda: normal.types() == ["pause"])
        await h.eng.idle()
        assert over.types() == []
        status = await (await cli()).request("status", profile_id="work")
        s = status["profiles"][0]["supervisor"]
        assert s["state"] == "paused" and s["resume_at"] == format_iso(RESET)
        snapshot = daemon.build_snapshot()
        assert snapshot is not None
        assert validate(snapshot, "widget-snapshot.schema.json") == []
        wsup = snapshot["profiles"][0]["supervisor"]
        assert wsup["state"] == "paused" and wsup["resume_at"] == format_iso(RESET)
        assert wsup["active_sessions"] == 2 and wsup["paused_sessions"] == 1
        await over.close()
        await normal.close()

    h.run(body)


def test_model_scoped_hold_follows_model(scen: Path) -> None:
    h = Harness()

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        opus, fable = FakeLauncher("opus"), FakeLauncher("fable")
        await opus.start()
        await fable.start()
        daemon.update_session("opus", lambda r: r.update(model_id="claude-opus-5-5"))
        daemon.update_session("fable", lambda r: r.update(model_id="claude-fable-5-1"))
        set_usage(scen, usage_payload((20, RESET), (30, WEEK), (("Fable", 96, WEEK),)))
        await daemon.request_poll("work")
        await wait_until(lambda: fable.types() == ["pause"])
        await h.eng.idle()
        assert opus.types() == []
        assert sup(daemon, "fable")["holds"] == ["model_scoped:Fable"]
        await opus.close()
        await fable.close()

    h.run(body)


def test_disabling_supervision_resumes(scen: Path, tmp_path: Path) -> None:
    h = Harness()

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: "work" in daemon.snapshots)
        w1 = FakeLauncher("w1", busy=True)
        await w1.start()
        set_usage(scen, usage_payload((95, RESET)))
        await daemon.request_poll("work")
        await wait_until(lambda: w1.types() == ["pause"])
        await h.eng.idle()
        c = await cli()
        cfg = json.loads(paths.config_file().read_text())
        cfg["profiles"][0]["supervisor"] = {"enabled": False}
        paths.config_file().write_text(json.dumps(cfg))
        await c.request("reload_config")
        await wait_until(lambda: "resume" in w1.types(), timeout=10)
        await h.eng.idle()
        assert w1.cmds[-1]["prompt"] == PROMPT
        assert (await c.request("pause", profile_id="work"))["error"] == "supervisor_disabled"
        await c.close()
        await w1.close()

    h.run(body)
