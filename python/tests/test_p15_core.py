"""P15 review fixes (ADR-0022): supervisor, usage, warm-up and config regressions."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import XdgDirs
from daemon_helpers import run_with_daemon, scenario, short_state_dir, write_config
from supervisor_helpers import FakeLauncher, set_usage, usage_payload

from ccs import paths
from ccs.cli import main
from ccs.clock import FakeClock, SystemClock
from ccs.config.defaults import default_config_dict, default_profile_dict
from ccs.config.models import Config, Profile
from ccs.config.validate import MODEL_RE, REQUIRED_KEYS, SLUG_RE, TIME_RE, validate
from ccs.daemon.hooks import WrapperInfo
from ccs.daemon.server import Daemon, new_session_record
from ccs.events import Event
from ccs.supervisor import engine as engine_mod
from ccs.supervisor import policy
from ccs.supervisor.cli import describe_control
from ccs.supervisor.model import (
    Hold,
    ProfileSupervisorState,
    SessionView,
    Supervision,
    extra_instance,
    upgrade_key,
)
from ccs.supervisor.policy import AdoptOverride, PauseSession, evaluate
from ccs.usage.model import ExtraUsage, ScopedWindow, UsageSnapshot, Window
from ccs.warmup import rules
from ccs.warmup.runner import ERROR, WarmupRunner
from ccs.warmup.state import WarmupStore

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=2)
FABLE_RESET = NOW + timedelta(days=2)
ARABIC_SEVEN = "0\u0667:00"  # isdigit() is true for non-ASCII digits
SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schema" / "config.schema.json"


def prof(**extra: Any) -> Profile:
    raw: dict[str, Any] = {
        "id": "work",
        "flag": "work",
        "name": "Work",
        "emoji": "💼",
        "config_dir": "/tmp/cfg-work",
    }
    raw.update(extra)
    return Profile.from_dict(raw)


def spill() -> Profile:
    return prof(limits={"extra_usage": {"spill": True, "warn": 80, "pause": 90}})


def snap(
    *,
    session: int | None = 20,
    fable: int | None = None,
    extra: ExtraUsage | None = None,
    at: datetime = NOW,
) -> UsageSnapshot:
    return UsageSnapshot(
        profile_id="work",
        status="ok",
        fetched_at=at,
        polled_at=at,
        session=Window(session, RESET, at) if session is not None else None,
        model_scoped=(ScopedWindow("Fable", fable, FABLE_RESET, at),) if fable is not None else (),
        extra_usage=extra,
    )


def credits(percent: int, limit: float = 100.0) -> ExtraUsage:
    return ExtraUsage(True, percent, limit * percent / 100, limit, "EUR", None)


def view(
    wid: str = "w1",
    *,
    model: str | None = None,
    state: str = "running",
    overridden: tuple[str, ...] = (),
    pending: tuple[str, ...] = (),
) -> SessionView:
    return SessionView(
        wrapper_id=wid,
        model_id=model,
        supervision=Supervision(
            state=state, overridden_instances=overridden, pending_override_instances=pending
        ),
    )


def fable_hold() -> Hold:
    return Hold(
        id="model_scoped:Fable",
        kind="model_scoped",
        instance=policy.instance_key("model_scoped", FABLE_RESET, "Fable"),
        scope="Fable",
        resets_at=FABLE_RESET,
        created_at=NOW,
    )


def kinds(d: policy.Decision, etype: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in d.events if e.type == etype]


# ---------------------------------------------------------------- fake daemon for the engine


class FakeDaemon:
    """The daemon surface `SupervisorEngine` uses (records in memory, state files on disk)."""

    def __init__(self, snapshot: UsageSnapshot | None) -> None:
        cfg = default_config_dict()
        cfg["profiles"] = [default_profile_dict("work", "work", "Work", "W", "/tmp/cfg-work")]
        cfg["default_profile"] = "work"
        self.config = Config.from_dict(cfg)
        self.clock = FakeClock(NOW)
        self.snapshots: dict[str, UsageSnapshot] = {"work": snapshot} if snapshot else {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: list[Event] = []
        self.cmds: list[tuple[str, str, dict[str, Any]]] = []
        self.snapshot_writes = 0

    def profile(self, pid: str) -> Profile | None:
        return self.config.profile(pid)

    def profile_ids(self) -> list[str]:
        return [p.id for p in self.config.profiles]

    def sessions_for(self, pid: str) -> list[dict[str, Any]]:
        return [r for r in self.sessions.values() if r.get("profile_id") == pid]

    def update_session(
        self, wid: str, mutate: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any] | None:
        rec = self.sessions.get(wid)
        if rec is not None:
            mutate(rec)
        return rec

    async def send_cmd(
        self, wid: str, cmd: str, payload: dict[str, Any], *, timeout: float = 0
    ) -> dict[str, Any]:
        self.cmds.append((wid, cmd, payload))
        return {"result": "skipped", "detail": {"was_busy": False}}

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def request_poll(self, pid: str, at: datetime | None = None) -> None:
        return None

    def request_snapshot_write(self) -> None:
        self.snapshot_writes += 1

    def add_session(self, wid: str) -> dict[str, Any]:
        rec = new_session_record(
            wrapper_id=wid, profile_id="work", wrapper_pid=1, claude_pid=2, cwd="/x", now=NOW
        )
        self.sessions[wid] = rec
        return rec


def info(rec: dict[str, Any], *, overridden: bool = False) -> WrapperInfo:
    return WrapperInfo(
        wrapper_id=rec["wrapper_id"],
        profile_id="work",
        wrapper_pid=1,
        claude_pid=2,
        cwd="/x",
        started_overridden=overridden,
        reregistered=False,
        record=rec,
    )


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CCS_STATE_DIR", str(tmp_path / "state"))
    paths.ensure_state_layout()
    return tmp_path / "state"


def make_engine(daemon: FakeDaemon) -> engine_mod.SupervisorEngine:
    return engine_mod.SupervisorEngine(cast(Any, daemon))


# ---------------------------------------------------------------- 1. started anyway


def test_policy_pending_model_scoped_instance_is_adopted_not_paused() -> None:
    hold = fable_hold()
    state = ProfileSupervisorState(holds=(hold,), paused_instances=(hold.instance,))
    s = view(model="claude-fable-5", pending=(hold.instance,))
    d = evaluate(prof(), snap(fable=96), state, [s], NOW)
    assert d.actions == (AdoptOverride("w1", (hold.instance,)),)


def test_policy_pending_instance_ignored_for_unrelated_or_unknown_model() -> None:
    hold = fable_hold()
    state = ProfileSupervisorState(holds=(hold,), paused_instances=(hold.instance,))
    for model in ("claude-opus-5-5", None):
        s = view(model=model, pending=(hold.instance,))
        assert evaluate(prof(), snap(fable=96), state, [s], NOW).actions == ()


def test_policy_new_model_scoped_instance_still_pauses_started_anyway_session() -> None:
    old = policy.instance_key("model_scoped", FABLE_RESET - timedelta(days=7), "Fable")
    s = view(model="claude-fable-5", pending=(old,))
    d = evaluate(prof(), snap(fable=96), ProfileSupervisorState(), [s], NOW)
    assert [type(a) for a in d.actions] == [PauseSession]


def test_engine_started_anyway_survives_model_becoming_known(state_dir: Path) -> None:
    """r3: `Start anyway` while a Fable hold is active; the statusline then reports Fable."""
    daemon = FakeDaemon(snap(fable=96))
    eng = make_engine(daemon)

    async def body() -> None:
        eng.evaluate("work")
        assert [h.id for h in eng.state("work").holds] == ["model_scoped:Fable"]
        fable, opus = daemon.add_session("w1"), daemon.add_session("w2")
        for rec in (fable, opus):
            eng.on_wrapper_registered(info(rec, overridden=True))
            # the model is unknown at registration: not shown as overridden yet
            assert rec["supervision"]["state"] == "running"
            assert rec["supervision"]["pending_override_instances"] == [fable_hold().instance]
        fable["model_id"] = "claude-fable-5"
        opus["model_id"] = "claude-opus-5-5"
        eng.evaluate("work")
        await eng.idle()
        eng.evaluate("work")
        await eng.idle()

    asyncio.run(body())
    assert daemon.cmds == []  # nobody is paused
    sup = daemon.sessions["w1"]["supervision"]
    assert sup["state"] == "overridden"
    assert sup["overridden_instances"] == [fable_hold().instance]
    assert sup["pending_override_instances"] == []
    assert daemon.sessions["w2"]["supervision"]["state"] == "running"  # unrelated model
    (ev,) = [e for e in daemon.events if e.type == "limit.override"]
    assert ev.data["wrapper_id"] == "w1" and ev.data["reason"] == "started_overridden"
    assert any(e.action == "override" for e in eng.state("work").ledger)


# ---------------------------------------------------------------- 2. whole-string patterns


def _cfg(**over: Any) -> dict[str, Any]:
    cfg = default_config_dict()
    p = default_profile_dict("work", "work", "Work", "💼", "~/.claude-work")
    for dotted, value in over.items():
        node: Any = p
        parts = dotted.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    cfg["profiles"] = [p]
    cfg["default_profile"] = p["id"] if isinstance(p["id"], str) else "work"
    return cfg


@pytest.mark.parametrize(
    ("over", "path"),
    [
        ({"id": "work\n"}, "profiles[0].id"),
        ({"flag": "work\n"}, "profiles[0].flag"),
        ({"warmup__active_hours__end": "23:00\n"}, "profiles[0].warmup.active_hours.end"),
        ({"warmup__active_hours__start": ARABIC_SEVEN}, "profiles[0].warmup.active_hours.start"),
        (
            {"warmup__triggers__schedule": [{"time": "06:00\n", "weekdays": ["mon"]}]},
            "profiles[0].warmup.triggers.schedule[0].time",
        ),
    ],
)
def test_patterns_match_the_whole_ascii_string(over: dict[str, Any], path: str) -> None:
    assert path in [i.path for i in validate(_cfg(**over))]


def test_parse_hhmm_whole_string_ascii() -> None:
    assert rules.parse_hhmm("06:05") == (6, 5)
    for bad in ("07:00\n", ARABIC_SEVEN, "7:00", "07:00 ", "24:00"):
        with pytest.raises(ValueError):
            rules.parse_hhmm(bad)


# ---------------------------------------------------------------- 3. orphaned manual hold


def test_policy_release_orphaned_holds() -> None:
    state = ProfileSupervisorState()
    state, profile_hold, _ = policy.add_manual_hold(state, NOW)
    state, gone, _ = policy.add_manual_hold(state, NOW, "w-gone")
    state, alive, _ = policy.add_manual_hold(state, NOW, "w-alive")
    new, dropped = policy.release_orphaned_holds(state, NOW, {"w-alive"})
    assert dropped == [gone]
    assert new.holds == (profile_hold, alive)
    last = new.ledger[-1]
    assert (last.action, last.detail, last.wrapper_ids) == ("release", "session_ended", ("w-gone",))
    assert policy.release_orphaned_holds(new, NOW, {"w-alive"}) == (new, [])


def test_engine_session_hold_released_on_unregister(state_dir: Path) -> None:
    """r4: `ccs pause --session w1`, then w1 exits: the profile must not stay paused."""
    daemon = FakeDaemon(snap())
    eng = make_engine(daemon)

    async def body() -> None:
        rec = daemon.add_session("w1")
        reply = await eng.op_pause(cast(Any, None), {"wrapper_id": "w1"})
        assert reply["ok"] and reply["sessions_paused"] == 1
        await eng.idle()
        del daemon.sessions["w1"]  # `Daemon.unregister` drops the record, then fires the hook
        writes = daemon.snapshot_writes
        eng.on_wrapper_unregistered(info(rec))
        assert daemon.snapshot_writes > writes

    asyncio.run(body())
    assert eng.state("work").holds == ()
    disk = json.loads(paths.supervisor_file("work").read_text())
    assert disk["holds"] == []
    daemon.clock.advance(3 * 86400)
    eng.on_tick(daemon.clock.now())
    assert eng.contribute("work")["state"] == "normal"


def test_engine_sweeps_session_hold_whose_record_vanished(state_dir: Path) -> None:
    """A record dropped at daemon start (dead launcher) never fires the unregister hook."""
    st, _, _ = policy.add_manual_hold(ProfileSupervisorState(), NOW, "w-dead")
    paths.supervisor_file("work").write_text(json.dumps(st.to_dict("work")))
    daemon = FakeDaemon(snap())
    eng = make_engine(daemon)
    assert [h.scope for h in eng.state("work").holds] == ["w-dead"]
    eng.evaluate("work")
    assert eng.state("work").holds == ()


@pytest.fixture
def scen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_state_dir(monkeypatch):
        scenario(tmp_path, monkeypatch, {})
        path = tmp_path / "fake-scenario.json"
        set_usage(path, usage_payload((20, RESET)))
        write_config(tmp_path)
        yield path


def test_reaper_releases_session_scoped_manual_hold(scen: Path) -> None:
    engines: list[engine_mod.SupervisorEngine] = []
    dead = subprocess.Popen(["true"])
    dead.wait()

    def install(daemon: Daemon) -> None:
        engines.append(engine_mod.install(daemon))

    async def body(daemon: Daemon) -> None:
        eng = engines[-1]
        w1 = FakeLauncher("w1", busy=False)
        await w1.start()
        reply = await eng.op_pause(cast(Any, None), {"wrapper_id": "w1"})
        assert reply["ok"] and [h.scope for h in eng.state("work").holds] == ["w1"]
        await eng.idle()
        daemon.sessions["w1"]["wrapper_pid"] = dead.pid  # the launcher died (e.g. kill -9)
        assert await daemon.reaper.reap_once() == ["w1"]
        assert eng.state("work").holds == ()
        assert daemon.supervisor_state("work")["state"] == "normal"
        disk = json.loads(paths.supervisor_file("work").read_text())
        assert disk["holds"] == []
        await w1.close()

    run_with_daemon(body, clock=FakeClock(NOW), extensions=[install])


# ---------------------------------------------------------------- 5. extra usage instance


def test_extra_usage_month_rollover_does_not_refire_or_repause() -> None:
    """r4: paused in September, resumed manually; October starts with the same credits."""
    p = spill()
    sep = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
    d = evaluate(p, snap(extra=credits(93), at=sep), ProfileSupervisorState(), [view()], sep)
    assert [h.instance for h in d.state.holds] == [extra_instance(100.0, 0)]
    assert {e.type for e in d.events} == {"limit.warn", "limit.pause"}
    st, _ = policy.release_holds(d.state, sep)  # ccs resume --profile work
    oct1 = datetime(2026, 10, 1, 0, 0, 5, tzinfo=UTC)
    d2 = evaluate(p, snap(extra=credits(93), at=oct1), st, [view()], oct1)
    assert d2.state.holds == () and d2.events == () and d2.actions == ()


def test_extra_usage_rearms_after_dropping_below_warn() -> None:
    p = spill()
    d1 = evaluate(p, snap(extra=credits(85)), ProfileSupervisorState(), [], NOW)
    assert len(kinds(d1, "limit.warn")) == 1 and d1.state.extra_usage_arm == 0
    # still above warn: no re-fire; between evaluations below warn the arm moves only once
    assert evaluate(p, snap(extra=credits(88)), d1.state, [], NOW).events == ()
    d2 = evaluate(p, snap(extra=credits(0)), d1.state, [], NOW)  # monthly reset
    assert d2.state.extra_usage_arm == 1 and d2.events == ()
    assert evaluate(p, snap(extra=credits(5)), d2.state, [], NOW).state == d2.state
    d3 = evaluate(p, snap(extra=credits(95)), d2.state, [view()], NOW)
    assert len(kinds(d3, "limit.warn")) == 1 and len(kinds(d3, "limit.pause")) == 1
    assert [h.instance for h in d3.state.holds] == [extra_instance(100.0, 1)]


def test_extra_usage_raised_cap_rearms() -> None:
    p = spill()
    d1 = evaluate(p, snap(extra=credits(95)), ProfileSupervisorState(), [view()], NOW)
    st, _ = policy.release_holds(d1.state, NOW)
    # same cap, still high: nothing; a raised cap (still above pause) is a new instance
    assert evaluate(p, snap(extra=credits(95)), st, [view()], NOW).events == ()
    d2 = evaluate(p, snap(extra=credits(92, limit=200.0)), st, [view()], NOW)
    assert [h.instance for h in d2.state.holds] == [extra_instance(200.0, 0)]
    assert len(kinds(d2, "limit.pause")) == 1


def test_extra_usage_arm_round_trips_and_legacy_keys_upgrade() -> None:
    st = ProfileSupervisorState(extra_usage_arm=3)
    assert ProfileSupervisorState.from_dict(st.to_dict("work")).extra_usage_arm == 3
    assert ProfileSupervisorState.from_dict({"extra_usage_arm": True}).extra_usage_arm == 0
    assert upgrade_key("warn:extra_usage:2026-09:100") == "warn:extra_usage:100:0"
    assert upgrade_key("extra_usage:2026-09:none") == "extra_usage:none:0"
    assert upgrade_key("session:2026-09-24T20:00Z") == "session:2026-09-24T20:00Z"
    legacy = {
        "holds": [],
        "warned": ["warn:extra_usage:2026-08:100", "warn:extra_usage:2026-09:100"],
        "paused_instances": ["extra_usage:2026-09:100"],
        "released_instances": ["extra_usage:2026-09:100"],
    }
    upgraded = ProfileSupervisorState.from_dict(legacy)
    assert upgraded.warned == ("warn:extra_usage:100:0",)
    assert upgraded.released_instances == ("extra_usage:100:0",)
    # right after the upgrade (a new calendar month, same credits): no spurious re-fire
    oct1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    d = evaluate(spill(), snap(extra=credits(93), at=oct1), upgraded, [view()], oct1)
    assert d.events == () and d.state.holds == ()
    held = {
        "holds": [
            {"id": "extra_usage", "kind": "extra_usage", "instance": "extra_usage:2026-09:100"}
        ]
    }
    (hold,) = ProfileSupervisorState.from_dict(held).holds
    assert hold.instance == "extra_usage:100:0"


# ---------------------------------------------------------------- 6. warm-up prompt / model


@pytest.mark.parametrize(
    "model",
    [
        "haiku",
        "sonnet",
        "opus",
        "opus[1m]",
        "claude-opus-5-5",
        "claude-opus-5-5[1m]",
        "claude-3-5-haiku-20241022",
        "us.anthropic.claude-opus-5-5-v1:0",
        "claude-opus-4-1@20250805",
    ],
)
def test_warmup_model_accepts_real_ids(model: str) -> None:
    assert validate(_cfg(warmup__model=model)) == []


@pytest.mark.parametrize(
    "model", ["", "-x", "--dangerously-skip-permissions", "haiku sonnet", "haiku\n", "a;b", "é"]
)
def test_warmup_model_rejects_unsafe(model: str) -> None:
    assert [i.path for i in validate(_cfg(warmup__model=model))] == ["profiles[0].warmup.model"]


@pytest.mark.parametrize(
    ("prompt", "ok"),
    [
        ("Reply with just: ok", True),
        ("Line one\n\tindented", True),
        ("-p", False),
        ("--help me", False),
        ("ok\x1b[201~", False),
        ("ok\x00", False),
        ("ok\r", False),
        ("\x85", False),
        ("  ", False),
    ],
)
def test_warmup_prompt_rules(prompt: str, ok: bool) -> None:
    issues = [i.path for i in validate(_cfg(warmup__prompt=prompt))]
    assert issues == ([] if ok else ["profiles[0].warmup.prompt"])


def test_resume_prompt_rejects_control_characters() -> None:
    cfg = _cfg(supervisor__resume_prompt="Continue\x1b[201~\r")
    assert [i.path for i in validate(cfg)] == ["profiles[0].supervisor.resume_prompt"]
    assert validate(_cfg(supervisor__resume_prompt="- continue\nplease")) == []


def test_runner_unexpected_error_still_finishes_the_attempt(tmp_xdg: XdgDirs) -> None:
    events: list[Event] = []

    async def poll(pid: str) -> UsageSnapshot | None:
        raise RuntimeError("boom")

    runner = WarmupRunner(
        clock=SystemClock(),
        store=WarmupStore(),
        emit=events.append,
        poll=poll,
        resolve_claude=lambda: "/usr/bin/true",
    )
    p = Profile.from_dict({**default_profile_dict("work", "work", "W", "W", "/tmp/cfg-work")})
    outcome = asyncio.run(runner.run(p, rules.MANUAL))
    assert outcome.result == "failed" and outcome.reason == ERROR
    assert outcome.detail == "RuntimeError: boom"
    assert [e.type for e in events] == ["warmup.started", "warmup.failed"]
    assert events[-1].data["reason"] == ERROR
    last = runner.store.view("work")["last_attempt"]
    assert last["result"] == "failed" and last["reason"] == ERROR
    assert not runner.is_running("work")


# ---------------------------------------------------------------- 7. resume hint


def test_pause_hint_names_the_same_target() -> None:
    cfg = Config.from_dict(_cfg())
    wid = "abcdef12-3456-7890-abcd-ef1234567890"
    session = {"profile_id": "work", "wrapper_id": wid, "created": True, "sessions_paused": 1}
    assert describe_control(cfg, "pause", session).endswith("ccs resume --session abcdef12")
    whole = {"profile_id": "work", "wrapper_id": None, "created": True, "sessions_paused": 2}
    assert describe_control(cfg, "pause", whole).endswith("ccs resume --profile work")


# ---------------------------------------------------------------- 8. config bounds + schema


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda c: c["polling"].update(interval_seconds=241), "polling.interval_seconds"),
        (
            lambda c: c["polling"].update(idle_interval_seconds=3600),
            "polling.idle_interval_seconds",
        ),
        (lambda c: c.update(claude_path="claude"), "claude_path"),
        (lambda c: c.update(claude_path=""), "claude_path"),
        (lambda c: c.update(ccs_path="bin/ccs"), "ccs_path"),
        (lambda c: c.update(ccs_path="/usr/local/bin/ccs\n"), "ccs_path"),
        (lambda c: c["profiles"][0].update(config_dir="~/.claude\x07"), "profiles[0].config_dir"),
        (lambda c: c["profiles"][0].update(name="Work\n"), "profiles[0].name"),
        (lambda c: c["profiles"][0].update(name="   "), "profiles[0].name"),
        (lambda c: c["profiles"][0].update(emoji="\x1b[31m"), "profiles[0].emoji"),
        (lambda c: c.pop("version"), "version"),
        (lambda c: c.pop("profiles"), "profiles"),
    ],
)
def test_config_bounds(mutate: Callable[[dict[str, Any]], Any], path: str) -> None:
    cfg = _cfg()
    mutate(cfg)
    assert path in [i.path for i in validate(cfg)]


def test_config_bounds_accept() -> None:
    cfg = _cfg()
    cfg["polling"]["idle_interval_seconds"] = 240
    cfg["claude_path"] = "~/.local/bin/claude"
    cfg["ccs_path"] = "/opt/ccs/bin/ccs"
    assert validate(cfg) == []


def _schema() -> dict[str, Any]:
    data = json.loads(SCHEMA_FILE.read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def _py(pattern: str) -> re.Pattern[str]:
    """An ECMAScript pattern in Python: `$` there never matches before a trailing newline."""
    assert pattern.startswith("^") and pattern.endswith("$"), pattern
    return re.compile(pattern[:-1] + r"\Z")


def _schema_ok(node: dict[str, Any], value: Any) -> bool:
    types = node.get("type", [])
    types = types if isinstance(types, list) else [types]
    if value is None:
        return "null" in types
    if isinstance(value, str):
        if "string" not in types or len(value) < node.get("minLength", 0):
            return False
        if len(value) > node.get("maxLength", len(value)):
            return False
        return "pattern" not in node or bool(_py(node["pattern"]).search(value))
    if isinstance(value, int) and not isinstance(value, bool):
        lo, hi = node.get("minimum", value), node.get("maximum", value)
        return "integer" in types and lo <= value <= hi
    raise AssertionError(value)


CASES: list[tuple[str, Any]] = [
    *[
        (f, v)
        for f in ("ccs_path", "claude_path")
        for v in (None, "", "claude", "/usr/bin/claude", "~/bin/claude", "/x\n", "/a\x9b")
    ],
    *[("polling.interval_seconds", v) for v in (4, 5, 60, 240, 241, 3600)],
    *[("p.id", v) for v in ("work", "work\n", "Work", "-w", "a" * 32, "a" * 33)],
    *[("p.flag", v) for v in ("lab", "lab\n", "la b")],
    *[("p.name", v) for v in ("Work", "", "  ", "Work\n", "Wo\trk", "Box ✓")],
    *[("p.emoji", v) for v in ("💼", "", " ", "\x07", "123456789")],
    *[("p.config_dir", v) for v in ("~/.claude", "/abs", "rel", "", "~/x\n")],
    *[
        ("p.warmup.model", v)
        for v in ("haiku", "opus[1m]", "claude-opus-5-5", "-x", "", "a b", "x\n")
    ],
    *[
        ("p.warmup.prompt", v)
        for v in ("Reply with just: ok", "a\n\tb", "-x", "", "  ", "a\x1b", "a\r")
    ],
    *[("p.supervisor.resume_prompt", v) for v in ("Go on.", "- go\n", "a\x1b", "", "\t")],
    *[
        ("p.warmup.active_hours.start", v)
        for v in ("07:00", "23:59", "24:00", "7:00", "07:00\n", ARABIC_SEVEN)
    ],
]


def _schema_node(schema: dict[str, Any], field: str) -> dict[str, Any]:
    parts = field.split(".")
    node: dict[str, Any] = schema
    if parts[0] == "p":
        node, parts = schema["$defs"]["profile"], parts[1:]
    for part in parts:
        node = node["properties"][part]
    return node


def _validator_ok(field: str, value: Any) -> bool:
    cfg = _cfg()
    parts = field.split(".")
    node: Any = cfg
    if parts[0] == "p":
        node, parts = cfg["profiles"][0], parts[1:]
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    if field == "p.id":
        cfg["default_profile"] = value
    return validate(cfg) == []


@pytest.mark.parametrize(("field", "value"), CASES)
def test_schema_and_validator_agree(field: str, value: Any) -> None:
    assert _schema_ok(_schema_node(_schema(), field), value) == _validator_ok(field, value)


def test_schema_required_matches_validator() -> None:
    assert tuple(_schema()["required"]) == REQUIRED_KEYS


def test_schema_patterns_are_the_validator_patterns() -> None:
    schema = _schema()
    prof_props = schema["$defs"]["profile"]["properties"]
    warm = prof_props["warmup"]["properties"]
    assert prof_props["id"]["pattern"] == f"^{SLUG_RE.pattern}$"
    assert prof_props["flag"]["pattern"] == f"^{SLUG_RE.pattern}$"
    assert warm["model"]["pattern"] == f"^{MODEL_RE.pattern}$"
    for node in (
        warm["active_hours"]["properties"]["start"],
        warm["active_hours"]["properties"]["end"],
    ):
        assert node["pattern"] == f"^{TIME_RE.pattern}$"
    sched = warm["triggers"]["properties"]["schedule"]["items"]["properties"]["time"]
    assert sched["pattern"] == f"^{TIME_RE.pattern}$"


def _all_patterns(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if isinstance(node.get("pattern"), str):
            yield node["pattern"]
        for sub in node.values():
            yield from _all_patterns(sub)
    elif isinstance(node, list):
        for sub in node:
            yield from _all_patterns(sub)


SAMPLES = [
    "work",
    "work\n",
    "07:00",
    "07:00\n",
    ARABIC_SEVEN,
    "~/x",
    "~/x\n",
    "/a\x9b",
    "-x",
    "a\n\tb",
    "a\x1b",
    "",
    " ",
    "opus[1m]",
    "us.anthropic.claude-opus-5-5-v1:0",
    "Box ✓",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_schema_patterns_are_ecmascript() -> None:
    """Each pattern compiles as a unicode ECMAScript regex and matches like `_py` does."""
    patterns = sorted(set(_all_patterns(_schema())))
    script = (
        "const [ps, ss] = JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(ps.map(p => ss.map(s => new RegExp(p, 'u').test(s)))));"
    )
    done = subprocess.run(
        ["node", "-e", script, json.dumps([patterns, SAMPLES])],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    expected = [[bool(_py(p).search(s)) for s in SAMPLES] for p in patterns]
    assert json.loads(done.stdout) == expected


# ---------------------------------------------------------------- 9. profile remove


@pytest.fixture
def lab(tmp_xdg: XdgDirs, tmp_home: Path) -> Path:
    (tmp_home / ".claude").mkdir()
    cdir = tmp_home / ".claude-lab"
    cdir.mkdir()
    (cdir / "settings.json").write_text('{"statusLine": {"type": "command", "command": "mine"}}')
    assert main(["profile", "add", "--id", "lab", "--emoji", "🧪", "--config-dir", str(cdir)]) == 0
    assert main(["statusline", "apply", "--profile", "lab"]) == 0
    assert paths.statusline_file("lab").exists()
    return cdir


def test_profile_remove_reverts_applied_statusline(
    lab: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["profile", "remove", "lab", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["removed"] == "lab" and doc["statusline"]["result"] == "reverted"
    settings = json.loads((lab / "settings.json").read_text())
    assert settings["statusLine"] == {"type": "command", "command": "mine"}
    assert not paths.statusline_file("lab").exists()


def test_profile_remove_on_conflict_still_removes_and_hints(
    lab: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (lab / "settings.json").write_text('{"statusLine": {"type": "command", "command": "edited"}}')
    capsys.readouterr()
    assert main(["profile", "remove", "lab"]) == 0
    out, err = capsys.readouterr()
    assert "removed profile 'lab'" in out
    assert err.startswith("ccs: ") and "statusLine" in err  # the revert hint
    cfg = json.loads(paths.config_file().read_text())
    assert "lab" not in [p["id"] for p in cfg["profiles"]]
    settings = json.loads((lab / "settings.json").read_text())
    assert settings["statusLine"]["command"] == "edited"  # the user's edit is untouched
    assert paths.statusline_file("lab").exists()  # the previous value stays recoverable
