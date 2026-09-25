#!/usr/bin/env python3
"""Anonymized demo environment for the README screenshots (see README.md next to this file).

Run it with the repo's venv (it imports `ccs`, installed editable there):

    python/.venv/bin/python scripts/screenshots/demo_env.py --root DIR [--exact]
        wipe and recreate DIR, then print the `export KEY=value` lines that point ccs at it
    … --root DIR --serve                 run a demo daemon on DIR until SIGTERM / Ctrl-C
    … --root DIR --launcher-prompt ID    print the launcher's start-while-paused question
    … --root DIR --sanitize OUT          map demo paths in OUT/* to `~`; exit 1 on any leak

Everything lives under DIR: a fake HOME (`home/`, with `.claude`, `.claude-work`,
`.claude-client`), `config/` (XDG_CONFIG_HOME) and `state/` (CCS_STATE_DIR). `claude` is the
repo's fake claude (python/tests/fake_claude) with one scenario per profile, and `launchctl`
is a stub, so nothing here reaches real Claude config dirs, real ccs state or launchd.
State is written through ccs's own serializers, the limit policy and the event bus, with
times relative to now.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
VENV_BIN = REPO / "python" / ".venv" / "bin"
FAKE_CLAUDE = REPO / "python" / "tests" / "fake_claude" / "claude"
SCHEMA_CHECK = REPO / "python" / "tests" / "schema_check.py"
MARKER = ".ccs-demo-root"
CLAUDE_VERSION = "2.1.281 (Claude Code)"
IDS = uuid.UUID("5c6f1d2e-8a4b-4c3d-9e2f-0a1b2c3d4e5f")  # namespace for stable demo ids

DAEMON_UPTIME = timedelta(hours=5, minutes=12)
LAST_POLL_AGO = timedelta(seconds=14)
WARN_AGO = timedelta(minutes=26)
PAUSE_AGO = timedelta(minutes=9)
WINDOW = timedelta(hours=5)

# Other (unsupervised) Claude sessions per profile, as the daemon's sampler would count them.
OTHER_SESSIONS = {
    "personal": {"interactive": 0, "background": 2},
    "work": {"interactive": 1, "background": 0},
    "client": {"interactive": 0, "background": 0},
}


@dataclass(frozen=True)
class DemoProfile:
    """One demo profile: identity, account, and its usage (resets as offsets from now)."""

    id: str
    name: str
    emoji: str
    config_dir: str
    email: str
    plan: str
    session: int
    session_in: timedelta
    weekly: int
    weekly_in: timedelta
    fable: int | None = None
    extra: dict[str, Any] | None = None


PROFILES = (
    DemoProfile(
        "personal", "Personal", "🏠", "~/.claude", "you@example.com", "max",
        session=45, session_in=timedelta(hours=2, minutes=13),
        weekly=62, weekly_in=timedelta(days=2, hours=5), fable=4,
        extra={"is_enabled": False, "monthly_limit": None, "used_credits": None,
               "utilization": None, "currency": None, "disabled_reason": None},
    ),
    DemoProfile(
        "work", "Work", "💼", "~/.claude-work", "you@work.example", "team",
        session=91, session_in=timedelta(minutes=42),
        weekly=71, weekly_in=timedelta(days=4, hours=3), fable=12,
        extra={"is_enabled": True, "monthly_limit": 1000, "used_credits": 320, "utilization": 32,
               "currency": "EUR", "disabled_reason": None, "decimal_places": 2},
    ),
    DemoProfile(
        "client", "Client", "🚀", "~/.claude-client", "you@client.example", "pro",
        session=18, session_in=timedelta(hours=3, minutes=51),
        weekly=23, weekly_in=timedelta(days=5, hours=20),
    ),
)  # fmt: skip
DEFAULT_PROFILE = "work"
WORK_WARN_PERCENT = 84  # the poll that crossed the warn threshold, before the pause


@dataclass(frozen=True)
class DemoSession:
    """One supervised `ccs` session."""

    key: str
    profile_id: str
    cwd: str  # relative to the fake HOME
    model_id: str
    activity: str
    busy_at_pause: bool | None


SESSIONS = (
    DemoSession("work-1", "work", "code/billing-service", "claude-opus-5-5", "idle", True),
    DemoSession("work-2", "work", "code/web-dashboard", "claude-fable-5-1", "idle", False),
    DemoSession("personal-1", "personal", "code/api", "claude-opus-5-5", "busy", None),
)


class DemoError(Exception):
    """Refused or failed; the message says why."""


# ---------------------------------------------------------------- layout and env


@dataclass(frozen=True)
class Layout:
    """The demo root and everything under it."""

    root: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def config_home(self) -> Path:
        return self.root / "config"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def scenarios(self) -> Path:
        return self.root / "fake-claude"

    @property
    def pid_file(self) -> Path:
        return self.run / "daemon.pid"

    def env(self) -> dict[str, str]:
        """The variables that point ccs (and `claude`, `launchctl`) at the demo."""
        demo_path = [str(self.bin), str(self.home / ".local" / "bin"), str(VENV_BIN)]
        rest = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in demo_path]
        return {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "CCS_STATE_DIR": str(self.state),
            "PATH": os.pathsep.join(demo_path + rest),
            "CCS_OSASCRIPT": "true",  # a demo event never posts a real notification
        }


def activate(layout: Layout) -> None:
    """Point this process at the demo (before any ccs path is resolved) and verify it."""
    os.environ.update(layout.env())
    for var in ("CLAUDE_CONFIG_DIR", "CCS_PROFILE", "CCS_WRAPPER_ID"):
        os.environ.pop(var, None)
    from ccs import paths

    roots = (layout.root, layout.root.resolve())
    for path in (
        paths.config_file(),
        paths.state_dir(),
        paths.expand_config_dir("~/.claude"),
    ):
        if not any(path.is_relative_to(root) for root in roots):
            raise DemoError(f"refusing: {path} is outside the demo root")


def resolve_root(value: str) -> Path:
    """The demo root as an absolute path; refuses anything that looks like real data."""
    root = Path(value).expanduser().absolute()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    resolved = root.resolve()
    if resolved in (Path("/"), real_home) or resolved in real_home.parents:
        raise DemoError(f"refusing to use {root} as the demo root")
    if resolved == REPO or REPO.is_relative_to(resolved):
        raise DemoError(f"refusing to use {root} (contains the repo)")
    if resolved.is_relative_to(real_home) and resolved != real_home:
        top = resolved.relative_to(real_home).parts[0]
        if top.startswith(".claude") or top in (".config", ".local", "Library"):
            raise DemoError(f"refusing to use {root} (inside ~/{top})")
    return root


def wipe(root: Path) -> None:
    """Remove a previous demo root: only one this script created (it has the marker)."""
    if not root.exists():
        return
    if not root.is_dir():
        raise DemoError(f"{root} exists and is not a directory")
    if any(root.iterdir()) and not (root / MARKER).is_file():
        raise DemoError(f"{root} is not empty and was not created by demo_env.py")
    shutil.rmtree(root)


# ---------------------------------------------------------------- time helpers


def reset_at(now: datetime, offset: timedelta, exact: bool, tz: tzinfo) -> datetime:
    """`now + offset`, rounded to the nearest local whole hour like Claude's resets; with
    `exact`, 30 s later instead, so the countdowns still read the offset (`in 2h 13m`) when
    the captures run a few seconds after the build."""
    t = now + offset
    if exact:
        return t + timedelta(seconds=30)
    local = (t + timedelta(minutes=30)).astimezone(tz).replace(minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


def stable_uuid(key: str) -> str:
    return str(uuid.uuid5(IDS, key))


def stable_pid(key: str) -> int:
    return 20000 + int(uuid.uuid5(IDS, "pid:" + key).hex[:4], 16) % 40000


# ---------------------------------------------------------------- usage payloads


@dataclass(frozen=True)
class Resets:
    session: datetime
    weekly: datetime


def usage_payload(p: DemoProfile, resets: Resets, session_percent: int) -> dict[str, Any]:
    """A `get_usage` response (`response.response`) in Claude Code's shape (P00-S2)."""
    weekly_iso = resets.weekly.isoformat()
    rate_limits: dict[str, Any] = {
        "five_hour": {
            "utilization": session_percent,
            "resets_at": resets.session.isoformat(),
        },
        "seven_day": {"utilization": p.weekly, "resets_at": weekly_iso},
        "model_scoped": (
            [{"display_name": "Fable", "utilization": p.fable, "resets_at": weekly_iso}]
            if p.fable is not None
            else []
        ),
        "extra_usage": p.extra,
    }
    return {
        "subscription_type": p.plan,
        "rate_limits_available": True,
        "rate_limits": rate_limits,
    }


def auth_json(p: DemoProfile, config_dir: str) -> dict[str, Any]:
    """`claude auth status --json` of a signed-in claude.ai account (P00-S4 fields)."""
    return {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "analyticsDisabled": False,
        "projectsDirectory": f"{config_dir}/projects",
        "configDirectory": config_dir,
        "email": p.email,
        "orgId": stable_uuid("org:" + p.id),
        "orgName": p.email,
        "subscriptionType": p.plan,
    }


# ---------------------------------------------------------------- build


def write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def ccs_python() -> str:
    """The interpreter the venv's `ccs` runs on (statusline scripts embed it; the doctor
    compares it), falling back to this interpreter."""
    from ccs.statusline import template

    try:
        found = template.parse_shebang((VENV_BIN / "ccs").read_text(encoding="utf-8"))
    except OSError:
        found = None
    return found if found and os.path.isabs(found) else sys.executable


def write_tools(layout: Layout, cfg: Any, resets: dict[str, Resets]) -> None:
    """The fake `claude` (per-profile scenarios) and the `launchctl` stub."""
    from ccs.paths import config_dir_env_value, is_default_claude_dir

    layout.scenarios.mkdir(parents=True)
    cases: list[str] = []
    for p in PROFILES:
        prof = cfg.profile(p.id)
        env_dir = config_dir_env_value(prof.config_dir)
        payload = usage_payload(p, resets[p.id], p.session)
        scenario = {
            "version": {"stdout": CLAUDE_VERSION},
            "auth_status": {"json": auth_json(p, env_dir)},
            "stream": {"get_usage": {"mode": "ok", "payload": payload}},
            "print": {"stdout": "ok"},
            "agents": {"sessions": []},
        }
        (layout.scenarios / f"{p.id}.json").write_text(json.dumps(scenario, indent=2) + "\n")
        patterns = [shlex.quote(env_dir)] + (
            ["''"] if is_default_claude_dir(prof.config_dir) else []
        )
        cases.append(f"  {' | '.join(patterns)}) scenario={p.id} ;;")
    (layout.scenarios / "unknown.json").write_text(
        json.dumps({"auth_status": {"json": {"loggedIn": False}, "exit": 1}}) + "\n"
    )
    q = shlex.quote
    write_exec(
        layout.home / ".local" / "bin" / "claude",
        "#!/bin/sh\n"
        "# Demo `claude` (scripts/screenshots/demo_env.py): the repo's fake claude, with the\n"
        "# scenario of the profile whose CLAUDE_CONFIG_DIR it runs with. Never the real claude.\n"
        'case "${CLAUDE_CONFIG_DIR-}" in\n'
        + "\n".join(cases)
        + "\n  *) scenario=unknown ;;\nesac\n"
        f'FAKE_CLAUDE_SCENARIO={q(str(layout.scenarios))}/"$scenario".json\n'
        f"FAKE_CLAUDE_LOG={q(str(layout.run / 'fake-claude.jsonl'))}\n"
        "export FAKE_CLAUDE_SCENARIO FAKE_CLAUDE_LOG\n"
        f'exec {q(sys.executable)} {q(str(FAKE_CLAUDE))} "$@"\n',
    )
    write_exec(
        layout.bin / "launchctl",
        "#!/bin/sh\n"
        "# Demo `launchctl` (scripts/screenshots/demo_env.py): never talks to the real launchd.\n"
        "# `print` reports the demo daemon (`demo_env.py --serve`) as the loaded LaunchAgent.\n"
        f"pidfile={q(str(layout.pid_file))}\n"
        'pid=$(cat "$pidfile" 2>/dev/null)\n'
        'if [ "$1" = print ] && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then\n'
        '    printf \'%s = {\\n\\tstate = running\\n\\tpid = %s\\n}\\n\' "$2" "$pid"\n'
        "    exit 0\n"
        "fi\n"
        'if [ "$1" = print ]; then\n'
        '    echo "Could not find service \\"${2##*/}\\" in domain for user gui: $(id -u)" >&2\n'
        "    exit 113\n"
        "fi\n"
        'echo "demo launchctl: refusing: launchctl $*" >&2\n'
        "exit 1\n",
    )


def config_dict(layout: Layout) -> dict[str, Any]:
    from ccs.config import defaults

    raw = defaults.default_config_dict()
    raw["revision"] = 7
    raw["default_profile"] = DEFAULT_PROFILE
    raw["claude_path"] = str(layout.home / ".local" / "bin" / "claude")
    raw["ccs_path"] = None
    for p in PROFILES:
        prof = defaults.default_profile_dict(p.id, p.id, p.name, p.emoji, p.config_dir)
        if p.id == "work":
            prof["warmup"]["triggers"]["schedule"] = [
                {"time": "07:30", "weekdays": ["mon", "tue", "wed", "thu", "fri"]}
            ]
            prof["warmup"]["triggers"]["auto_chain"] = True
        raw["profiles"].append(prof)
    return raw


def build(layout: Layout, *, exact: bool) -> None:
    """Wipe and recreate the whole demo environment under `layout.root`."""
    wipe(layout.root)
    layout.root.mkdir(parents=True)
    (layout.root / MARKER).write_text("created by scripts/screenshots/demo_env.py\n")
    activate(layout)

    from ccs import __version__, fsio, paths
    from ccs.clock import FakeClock, SystemClock, local_tz
    from ccs.config import store
    from ccs.daemon import launchd
    from ccs.daemon.scheduler import CHAIN_DELAY, next_occurrence
    from ccs.daemon.server import new_session_record
    from ccs.events import Event, EventBus
    from ccs.snapshot import build_widget_snapshot, write_widget_snapshot
    from ccs.statusline import template
    from ccs.supervisor import policy
    from ccs.supervisor.model import ProfileSupervisorState, SessionView
    from ccs.usage.model import UsageSnapshot, format_iso, to_utc_seconds
    from ccs.usage.normalize import normalize
    from ccs.usage.source_claude import write_snapshot
    from ccs.warmup import rules
    from ccs.warmup.state import SUCCEEDED, WarmupStore

    tz = local_tz()
    now = to_utc_seconds(SystemClock().now())

    # fake HOME: Claude config dirs, and the XDG defaults linked to config/ and state/
    home = layout.home
    for p in PROFILES:
        (home / p.config_dir.removeprefix("~/")).mkdir(parents=True)
    (home / ".local" / "state").mkdir(parents=True)
    layout.config_home.mkdir()
    layout.run.mkdir()
    (home / ".config").symlink_to(os.path.relpath(layout.config_home, home))
    state_link = home / ".local" / "state" / "ccs"
    state_link.symlink_to(os.path.relpath(layout.state, state_link.parent))
    paths.ensure_state_layout()

    cfg = store.create(config_dict(layout))
    resets = {
        p.id: Resets(
            reset_at(now, p.session_in, exact, tz),
            reset_at(now, p.weekly_in, exact, tz),
        )
        for p in PROFILES
    }
    write_tools(layout, cfg, resets)

    # an installed-looking app and LaunchAgent (the `launchctl` stub keeps launchd out of it)
    (home / "Applications" / "CC Supervisor.app" / "Contents").mkdir(parents=True)
    plist = home / "Library" / "LaunchAgents" / f"{launchd.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(
        launchd.render_plist(str(VENV_BIN / "ccs"), launchd.install_env(), paths.state_dir())
    )
    python = ccs_python()
    for prof in cfg.profiles:
        template.generate(prof, cfg, python=python)

    # usage: raw payloads through the real normalizer
    def snap_at(p: DemoProfile, when: datetime, session: int | None = None) -> UsageSnapshot:
        payload = usage_payload(p, resets[p.id], p.session if session is None else session)
        return normalize(payload, profile_id=p.id, fetched_at=when)

    snapshots = {p.id: snap_at(p, now - LAST_POLL_AGO) for p in PROFILES}
    for snap in snapshots.values():
        write_snapshot(snap)

    # sessions
    work_reset = resets["work"].session
    started = {
        "work-1": work_reset - WINDOW + timedelta(minutes=17),
        "work-2": now - timedelta(hours=1, minutes=48),
        "personal-1": now - timedelta(hours=1, minutes=2),
    }
    records: dict[str, dict[str, Any]] = {}
    for s in SESSIONS:
        rec = new_session_record(
            wrapper_id=stable_uuid("wrapper:" + s.key),
            profile_id=s.profile_id,
            wrapper_pid=stable_pid("wrapper:" + s.key),
            claude_pid=stable_pid("claude:" + s.key),
            cwd=str(home / s.cwd),
            now=started[s.key],
        )
        rec["session_id"] = stable_uuid("session:" + s.key)
        rec["model_id"] = s.model_id
        rec["activity"] = s.activity
        records[s.key] = rec

    boot = {"pid": 4242, "version": __version__}
    events = [(now - DAEMON_UPTIME, Event("daemon.started", None, None, boot))]
    for key, rec in records.items():
        data = {
            "wrapper_id": rec["wrapper_id"],
            "cwd": rec["cwd"],
            "claude_pid": rec["claude_pid"],
        }
        events.append((started[key], Event("session.started", rec["profile_id"], None, data)))

    # the work limit story, through the real policy: warn at 84%, pause at 91%
    profiles = {p.id: p for p in PROFILES}
    work = cfg.profile("work")
    if work is None:
        raise DemoError("the demo config has no work profile")
    work_recs = [r for r in records.values() if r["profile_id"] == "work"]
    views = [SessionView.from_record(r) for r in work_recs]
    warn_at, pause_at = now - WARN_AGO, now - PAUSE_AGO
    warned = policy.evaluate(
        work,
        snap_at(profiles["work"], warn_at, WORK_WARN_PERCENT),
        ProfileSupervisorState(),
        [v for v in views if v is not None],
        warn_at,
    )
    if warned.actions or [e.type for e in warned.events] != ["limit.warn"]:
        raise DemoError(f"unexpected warn decision: {warned}")
    events += [(warn_at, e) for e in warned.events]
    paused = policy.evaluate(
        work,
        snap_at(profiles["work"], pause_at),
        warned.state,
        [v for v in views if v is not None],
        pause_at,
    )
    events += [(pause_at, e) for e in paused.events]
    by_wrapper = {r["wrapper_id"]: (k, r) for k, r in records.items()}
    for action in paused.actions:
        if not isinstance(action, policy.PauseSession):
            raise DemoError(f"unexpected supervisor action: {action}")
        key, rec = by_wrapper[action.wrapper_id]
        rec["supervision"].update(
            state="paused",
            holds=list(action.hold_ids),
            paused_at=format_iso(pause_at),
            resume_at=format_iso(action.resume_at),
            was_busy_at_pause=next(s.busy_at_pause for s in SESSIONS if s.key == key),
        )
    work_state = paused.state
    for rec in records.values():
        fsio.atomic_write_json(paths.session_file(rec["wrapper_id"]), rec, mode=0o644)
    fsio.atomic_write_json(paths.supervisor_file("work"), work_state.to_dict("work"))

    # the policy must agree that the written state is settled (no further action or event)
    states = {p.id: ProfileSupervisorState() for p in PROFILES} | {"work": work_state}
    for prof in cfg.profiles:
        pviews = [
            SessionView.from_record(r) for r in records.values() if r["profile_id"] == prof.id
        ]
        check = policy.evaluate(
            prof, snapshots[prof.id], states[prof.id], [v for v in pviews if v], now
        )
        if check.actions or check.events or check.state != states[prof.id]:
            raise DemoError(f"demo state for {prof.id} is not settled: {check}")

    # warm-ups: the window starts, plus the next planned run (as the scheduler computes it)
    store_w = WarmupStore()
    warmups = {
        "work": (resets["work"].session - WINDOW, rules.AUTO_CHAIN, True),
        "personal": (resets["personal"].session - WINDOW, rules.UNLOCK_WAKE, True),
        "client": (now - timedelta(days=1, hours=3), rules.APP_START, False),
    }
    for pid, (start, trigger, logged) in warmups.items():
        begun, done = start - timedelta(seconds=3), start + timedelta(seconds=2)
        store_w.begin_attempt(pid, begun, trigger)
        store_w.finish_attempt(
            pid,
            at=begun,
            trigger=trigger,
            result=SUCCEEDED,
            reason=None,
            resets_at=start + WINDOW,
            finished_at=done,
        )
        if logged:
            wprof = cfg.profile(pid)
            if wprof is None:
                raise DemoError(f"the demo config has no {pid} profile")
            model = wprof.warmup.model
            events.append(
                (
                    begun,
                    Event(
                        "warmup.started",
                        pid,
                        None,
                        {"trigger": trigger, "model": model},
                    ),
                )
            )
            key = f"warmup:{pid}:{begun.isoformat(timespec='microseconds')}"
            data = {"trigger": trigger, "resets_at": format_iso(start + WINDOW)}
            events.append((done, Event("warmup.succeeded", pid, key, data)))
    next_warmups: dict[str, datetime | None] = {}
    for prof in cfg.profiles:
        candidates: list[tuple[datetime, str]] = []
        triggers, hours = prof.warmup.triggers, prof.warmup.active_hours
        if triggers.schedule:
            occ = next_occurrence(triggers.schedule, now, tz)
            if occ is not None:
                candidates.append((occ, rules.SCHEDULE))
        fire = resets[prof.id].session + CHAIN_DELAY
        if triggers.auto_chain and rules.in_active_hours(fire, hours.start, hours.end, tz):
            candidates.append((fire, rules.AUTO_CHAIN))
        when: datetime | None = None
        next_trigger: str | None = None
        if candidates:
            when, next_trigger = min(candidates)
        store_w.set_next(prof.id, when, next_trigger)
        next_warmups[prof.id] = when

    # events.jsonl through the real bus (titles, bodies, notify flags, dedupe keys)
    clock = FakeClock(min(ts for ts, _ in events))
    bus = EventBus(clock=clock, config=lambda: cfg)
    for ts, event in sorted(events, key=lambda item: item[0]):
        clock.set(ts)
        bus.emit(event)

    # widget/snapshot.json through the real builder
    sup_states = {
        prof.id: {
            "state": policy.supervisor_state_label(
                prof, snapshots[prof.id], states[prof.id].holds, now
            ),
            "resume_at": policy.profile_resume_at(states[prof.id].holds),
        }
        for prof in cfg.profiles
    }
    sessions_by_profile = {
        pid: sorted(
            (r for r in records.values() if r["profile_id"] == pid),
            key=lambda r: r["started_at"],
        )
        for pid in profiles
    }
    doc = build_widget_snapshot(
        cfg,
        snapshots,
        sup_states,
        sessions_by_profile,
        OTHER_SESSIONS,
        next_warmups,
        now,
    )
    write_widget_snapshot(doc)
    check_schemas(layout)


def check_schemas(layout: Layout) -> None:
    """Validate the written state against schema/*.schema.json (the tests' checker)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ccs_demo_schema_check", SCHEMA_CHECK)
    if spec is None or spec.loader is None:
        print(f"demo_env: {SCHEMA_CHECK} missing; schemas not checked", file=sys.stderr)
        return
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    state = layout.state
    docs: list[tuple[str, Any]] = [
        (
            "config.schema.json",
            json.loads((layout.config_home / "ccs/config.json").read_text()),
        ),
        (
            "widget-snapshot.schema.json",
            json.loads((state / "widget/snapshot.json").read_text()),
        ),
    ]
    for sub, schema in (
        ("usage", "usage-snapshot.schema.json"),
        ("sessions", "session-record.schema.json"),
        ("supervisor", "supervisor-state.schema.json"),
    ):
        docs += [(schema, json.loads(f.read_text())) for f in sorted((state / sub).glob("*.json"))]
    for line in (state / "events.jsonl").read_text().splitlines():
        docs.append(("event.schema.json", json.loads(line)))
    problems = [f"{name}: {err}" for name, doc in docs for err in checker.validate(doc, name)]
    if problems:
        raise DemoError("state does not match the schemas:\n  " + "\n  ".join(problems))


# ---------------------------------------------------------------- demo daemon


def serve(layout: Layout) -> int:
    """The real daemon (supervisor engine + poller against the fake claude) on the demo state,
    minus what would rewrite the demo story: no sampler or reaper, fake launcher pids kept,
    its own events logged under run/, and demo_env's widget snapshot left as built."""
    if not (layout.root / MARKER).is_file():
        raise DemoError(f"{layout.root} is not a demo root: run demo_env.py --root first")
    activate(layout)
    from ccs import fsio, paths
    from ccs.daemon.client import AsyncDaemonClient, DaemonUnavailable
    from ccs.daemon.server import Daemon, DaemonAlreadyRunning
    from ccs.events import EventBus
    from ccs.supervisor import engine
    from ccs.usage.model import parse_time

    sock = paths.daemon_sock()
    if len(str(sock).encode()) > 100:
        raise DemoError(f"socket path too long for macOS ({sock}): use a shorter --root")
    logging.basicConfig(
        filename=layout.run / "daemon.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    class DemoDaemon(Daemon):
        def _load_session(self, path: Path) -> None:
            rec = fsio.read_json(path)  # the demo launchers' pids are made up: keep them
            if rec is not None and paths.is_valid_id(rec.get("wrapper_id")):
                self.sessions[rec["wrapper_id"]] = rec

        def write_snapshot_now(self) -> dict[str, Any] | None:
            return self.build_snapshot()

    def next_warmup(profile_id: str) -> dict[str, Any]:
        doc = fsio.read_json(paths.warmup_file(profile_id)) or {}
        return {"next_warmup_at": parse_time(doc.get("next_scheduled_at"))}

    async def app_connection() -> None:
        client = AsyncDaemonClient()
        with contextlib.suppress(DaemonUnavailable):
            await client.connect()
            await client.hello(client="app")  # the menu bar app: native notifications
            while await client.next_push() is not None:
                pass

    async def ready(daemon: Daemon) -> None:
        while daemon.config is None:  # installed before the config is set
            await asyncio.sleep(0.05)
        await asyncio.gather(*(daemon.request_poll(pid) for pid in daemon.profile_ids()))
        layout.pid_file.write_text(f"{os.getpid()}\n")

    def install(daemon: Daemon) -> None:
        daemon.started_at = daemon.clock.now() - DAEMON_UPTIME
        daemon.other_sessions = {pid: dict(c) for pid, c in OTHER_SESSIONS.items()}
        daemon.hooks.contribute_supervisor(next_warmup)
        daemon.add_task(app_connection)
        daemon.add_task(lambda: ready(daemon))

    daemon = DemoDaemon(
        extensions=[engine.install, install], enable_sampler=False, enable_reaper=False
    )
    daemon.events = EventBus(
        clock=daemon.clock,
        config=lambda: daemon.config,
        events_path=layout.run / "daemon-events.jsonl",
        seen_path=layout.run / "daemon-events.seen.json",
    )
    try:
        asyncio.run(daemon.run())
    except DaemonAlreadyRunning:
        print("demo daemon already running", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        layout.pid_file.unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------- launcher prompt


def launcher_prompt(layout: Layout, profile_id: str) -> int:
    """The exact question `ccs --<flag>` asks while the profile is paused (no newline).

    Same calls as the launcher: the daemon's `status` reply for the profile, then
    `prompt.held_info` and `prompt.held_prompt`. Without a demo daemon the reply is rebuilt
    from `supervisor/<id>.json`.
    """
    activate(layout)
    from ccs import fsio, paths
    from ccs.clock import SystemClock, local_tz
    from ccs.config import store
    from ccs.daemon.client import DaemonClient, DaemonUnavailable
    from ccs.launcher import prompt
    from ccs.supervisor import policy
    from ccs.supervisor.model import ProfileSupervisorState
    from ccs.usage.model import format_iso

    cfg = store.load()[0]
    profile = cfg.profile(profile_id)
    if profile is None:
        raise DemoError(f"unknown profile '{profile_id}'")
    reply: dict[str, Any] | None = None
    try:
        with DaemonClient(timeout=2.0) as client:
            if client.hello().get("ok"):
                reply = client.request("status", profile_id=profile_id)
    except DaemonUnavailable:
        reply = None
    if not reply or not reply.get("ok"):
        state = ProfileSupervisorState.from_dict(fsio.read_json(paths.supervisor_file(profile_id)))
        sup = {
            "holds": [h.public() for h in state.holds],
            "resume_at": format_iso(policy.profile_resume_at(state.holds)),
        }
        reply = {"ok": True, "profiles": [{"id": profile_id, "supervisor": sup}]}
    info = prompt.held_info(reply, profile_id)
    if info is None:
        raise DemoError(f"profile {profile_id} is not paused")
    sys.stdout.write(prompt.held_prompt(profile.name, info, SystemClock().now(), local_tz()))
    return 0


# ---------------------------------------------------------------- sanitize


ALLOWED_EMAIL = re.compile(r"(?:.+\.)?example(?:\.(?:com|org|net))?", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def path_map(layout: Layout) -> list[tuple[str, str]]:
    """Literal replacements, longest source first: demo paths → `~` forms, repo → ~/Code."""
    pairs: dict[str, str] = {}
    for root in {str(layout.root), str(layout.root.resolve())}:
        pairs[f"{root}/state"] = "~/.local/state/ccs"
        pairs[f"{root}/config"] = "~/.config"
        pairs[f"{root}/home"] = "~"
        pairs[root] = "~"
    for repo in {str(REPO), os.path.realpath(REPO)}:
        pairs[repo] = "~/Code/cc-supervisor"
    return sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True)


def personal_markers() -> list[str]:
    """Strings that must never appear in a capture."""
    user = pwd.getpwuid(os.getuid())
    marks = {user.pw_name, user.pw_dir, "/Users/", "@gmail", os.environ.get("USER", "")}
    for key in ("user.email", "user.name"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(
                ["git", "-C", str(REPO), "config", key],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            marks.add(out.stdout.strip())
    host = socket.gethostname().split(".")[0]
    if len(host) >= 6 and host.lower() != "localhost":
        marks.add(host)
    return sorted(m for m in marks if len(m) >= 3)


def sanitize(layout: Layout, out_dir: Path) -> int:
    """Rewrite demo paths in every file of `out_dir`; report leaks (exit 1 if any)."""
    pairs = path_map(layout)
    marks = personal_markers()
    problems: list[str] = []
    for path in sorted(p for p in out_dir.iterdir() if p.is_file()):
        text = path.read_text(encoding="utf-8")
        for src, dst in pairs:
            text = text.replace(src, dst)
        path.write_text(text, encoding="utf-8")
        low = text.lower()
        problems += [f"{path.name}: contains {m!r}" for m in marks if m.lower() in low]
        for match in EMAIL.finditer(text):
            if not ALLOWED_EMAIL.fullmatch(match.group(1)):
                problems.append(f"{path.name}: non-example email {match.group(0)!r}")
    for problem in problems:
        print(f"LEAK {problem}", file=sys.stderr)
    return 1 if problems else 0


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", required=True, metavar="DIR", help="the demo root (wiped)")
    parser.add_argument(
        "--exact",
        action="store_true",
        help="exact reset offsets (default: whole hours)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="run the demo daemon")
    mode.add_argument("--launcher-prompt", metavar="ID", help="print the paused-start question")
    mode.add_argument("--sanitize", metavar="OUT", help="anonymize and leak-check OUT/*")
    args = parser.parse_args(argv)
    try:
        layout = Layout(resolve_root(args.root))
        if args.serve:
            return serve(layout)
        if args.launcher_prompt:
            return launcher_prompt(layout, args.launcher_prompt)
        if args.sanitize:
            return sanitize(layout, Path(args.sanitize))
        build(layout, exact=args.exact)
    except DemoError as exc:
        print(f"demo_env: {exc}", file=sys.stderr)
        return 1
    for key, value in layout.env().items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
