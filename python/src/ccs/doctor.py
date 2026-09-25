"""`ccs doctor` (P13): read-only diagnostics, each with a fix hint.

Every check returns `Check`s (`ok|warn|fail`), never raises and never writes anything: no
config seeding, no state layout, no settings changes. Checks run in parallel daemon threads
with a deadline, so a hung subprocess or socket becomes a `fail` instead of a hang.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import ccs
from ccs import __version__, auth, claude_cli, paths
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.config.validate import CLAUDE_LONG_OPTIONS, Issue, validate
from ccs.daemon.launchd import Launchd
from ccs.fsio import read_json
from ccs.output import EXIT_ERROR, EXIT_OK, emit_json
from ccs.statusline import apply as statusline_apply
from ccs.statusline import template
from ccs.usage.model import parse_time

Status = Literal["ok", "warn", "fail"]

APP_NAME = "CC Supervisor.app"
SUBPROCESS_TIMEOUT_S = 10.0
CHECK_TIMEOUT_S = 20.0
SOCKET_LATENCY_MAX_MS = 1000.0
POLL_LIVENESS_S = 5 * 60
DATA_FRESHNESS_S = 10 * 60
SNAPSHOT_FRESHNESS_S = 5 * 60
HOLD_OVERDUE_S = 10 * 60
MIN_PYTHON = (3, 12)

# Option-definition lines of `claude --help`, e.g. "  -c, --continue" or
# "  --allowedTools, --allowed-tools <tools...>".
_HELP_DEF_RE = re.compile(r"^\s+(?:-[A-Za-z], )?(--[A-Za-z][\w-]*(?:, --[A-Za-z][\w-]*)*)")
_HELP_OPT_RE = re.compile(r"--([A-Za-z][\w-]*)")
# The only state dir the sandboxed widgets can read (ADR-0012, ADR-0022).
DEFAULT_STATE_DIR = "~/.local/state/ccs"


@dataclass(frozen=True)
class Check:
    """One diagnostic result. `scope` is `global` or `profile:<id>`."""

    id: str
    scope: str
    status: Status
    message: str
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "status": self.status,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class Ran:
    """Result of a doctor subprocess (`rc` is -1 when it could not run)."""

    rc: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Mapping[str, str] | None, float], Ran]


def default_runner(
    argv: Sequence[str],
    env: Mapping[str, str] | None,
    timeout: float,
    *,
    cwd: str | None = None,
) -> Ran:
    """`subprocess.run` with a timeout; stdin closed so nothing can block on input."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=dict(env) if env is not None else None,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return Ran(-1, "", f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return Ran(-1, "", str(exc))
    return Ran(done.returncode, done.stdout, done.stderr)


def _socket_probe() -> dict[str, Any] | None:
    from ccs.daemon.launchd import socket_probe

    return socket_probe(timeout=1.0)


def _daemon_status() -> dict[str, Any] | None:
    from ccs.daemon.client import DaemonClient, DaemonUnavailable

    try:
        with DaemonClient(timeout=2.0) as client:
            client.hello()
            reply = client.request("status")
    except DaemonUnavailable:
        return None
    return reply if reply.get("ok") else None


def _first_json_object(text: str) -> Any:
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _auth_status(claude: str, profile: Profile, timeout: float) -> auth.AuthStatus | None:
    """`claude auth status --json` (exit 1 + JSON when logged out), parsed.

    Runs from the temp dir: the probes' default cwd (`warmup/cwd`) would be created in the
    state dir, and the doctor never writes.
    """
    ran = default_runner(
        [claude, "auth", "status", "--json"],
        claude_cli.profile_env(profile),
        timeout,
        cwd=tempfile.gettempdir(),
    )
    try:
        raw = _first_json_object(ran.stdout)
    except ValueError:
        return None
    return auth.parse_auth_status(raw)


@dataclass
class Env:
    """What the checks may touch. Tests replace the IO callables."""

    run: Runner = default_runner
    launchd: Launchd = field(default_factory=Launchd)
    socket_probe: Callable[[], dict[str, Any] | None] = _socket_probe
    daemon_status: Callable[[], dict[str, Any] | None] = _daemon_status
    auth_status: Callable[[str, Profile, float], auth.AuthStatus | None] = _auth_status
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    app_path: Path = field(
        default_factory=lambda: Path(os.path.expanduser("~/Applications")) / APP_NAME
    )
    subprocess_timeout: float = SUBPROCESS_TIMEOUT_S
    check_timeout: float = CHECK_TIMEOUT_S


@dataclass
class Ctx:
    """Shared, read-only facts gathered once before the checks run."""

    env: Env
    config_path: Path
    config: Config | None
    config_error: str | None
    issues: list[Issue]
    claude: str | None
    claude_error: str | None


def build_ctx(env: Env | None = None) -> Ctx:
    """Load (never seed) the config and resolve `claude`."""
    env = env or Env()
    config_path = paths.config_file()
    config: Config | None = None
    config_error: str | None = None
    issues: list[Issue] = []
    try:
        raw = store.load_raw(config_path)
    except store.ConfigMissing:
        config_error = "missing"
    except store.ConfigInvalid as exc:
        config_error = "invalid"
        issues = list(exc.issues)
    else:
        issues = validate(raw)
        if issues:
            config_error = "invalid"
        else:
            config = Config.from_dict(raw)
    claude: str | None = None
    claude_error: str | None = None
    try:
        claude = claude_cli.resolve_claude(config)
    except claude_cli.ClaudeNotFound as exc:
        claude_error = str(exc)
    return Ctx(env, config_path, config, config_error, issues, claude, claude_error)


# ---------------------------------------------------------------- helpers


def _age(seconds: float) -> str:
    """`42s`, `5m`, `2h 3m`, `3d 4h` (floored)."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _since(ctx: Ctx, value: Any) -> float | None:
    dt = parse_time(value)
    if dt is None:
        return None
    return (ctx.env.now() - dt).total_seconds()


def parse_help_options(text: str) -> set[str]:
    """Long option names defined in `claude --help` (definition lines only)."""
    found: set[str] = set()
    for line in text.splitlines():
        match = _HELP_DEF_RE.match(line)
        if match:
            found.update(_HELP_OPT_RE.findall(match.group(1)))
    return found


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _plist_path_env(plist: Path) -> str | None:
    """`PATH` captured into the LaunchAgent at `ccs daemon install`."""
    try:
        with plist.open("rb") as fh:
            doc = plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    env = doc.get("EnvironmentVariables") if isinstance(doc, dict) else None
    value = env.get("PATH") if isinstance(env, dict) else None
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------- global checks

G = "global"


def check_python(ctx: Ctx) -> list[Check]:
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro} ({sys.executable})"
    if (v.major, v.minor) >= MIN_PYTHON:
        return [Check("python.version", G, "ok", text)]
    return [Check("python.version", G, "fail", f"{text}: need >= 3.12", "make install-dev")]


def check_ccs(ctx: Ctx) -> list[Check]:
    source = Path(ccs.__file__).resolve().parent
    return [Check("ccs.version", G, "ok", f"ccs {__version__} ({source})")]


def check_claude_found(ctx: Ctx) -> list[Check]:
    out: list[Check] = []
    if ctx.claude is None:
        out.append(
            Check(
                "claude.found",
                G,
                "fail",
                ctx.claude_error or "claude not found",
                "install Claude Code, or: ccs config set claude_path=/path/to/claude",
            )
        )
    else:
        out.append(Check("claude.found", G, "ok", ctx.claude))
    plist = ctx.env.launchd.plist
    configured = ctx.config is not None and bool(ctx.config.claude_path)
    if plist.exists() and not configured:
        daemon_path = _plist_path_env(plist)
        if daemon_path is None:
            out.append(
                Check(
                    "claude.daemon_path",
                    G,
                    "warn",
                    "the LaunchAgent has no captured PATH",
                    "ccs daemon install",
                )
            )
        elif shutil.which("claude", path=daemon_path) is None:
            out.append(
                Check(
                    "claude.daemon_path",
                    G,
                    "fail",
                    "the daemon's PATH (captured at install) doesn't find claude",
                    "ccs daemon install  (re-captures PATH), "
                    "or: ccs config set claude_path=/path/to/claude",
                )
            )
        else:
            out.append(Check("claude.daemon_path", G, "ok", "the daemon's PATH finds claude"))
    return out


def check_claude_version(ctx: Ctx) -> list[Check]:
    if ctx.claude is None:
        return [Check("claude.version", G, "warn", "skipped: claude not found")]
    ran = ctx.env.run([ctx.claude, "--version"], None, ctx.env.subprocess_timeout)
    first = ran.stdout.strip().splitlines()[0] if ran.stdout.strip() else ""
    if ran.rc == 0 and first:
        return [Check("claude.version", G, "ok", first)]
    detail = ran.stderr.strip()[:200] or f"exit {ran.rc}"
    return [Check("claude.version", G, "warn", f"unknown version ({detail})")]


def check_config(ctx: Ctx) -> list[Check]:
    if ctx.config_error == "missing":
        return [
            Check(
                "config.valid",
                G,
                "fail",
                f"no config at {ctx.config_path}",
                "ccs profile list  (creates it with the default profiles)",
            )
        ]
    if ctx.config_error == "invalid":
        shown = "; ".join(f"{i.path or '(file)'}: {i.message}" for i in ctx.issues[:3])
        more = f" (+{len(ctx.issues) - 3} more)" if len(ctx.issues) > 3 else ""
        return [
            Check(
                "config.valid",
                G,
                "fail",
                f"{len(ctx.issues)} issue(s): {shown}{more}",
                "ccs config validate",
            )
        ]
    assert ctx.config is not None
    ids = ", ".join(p.id for p in ctx.config.profiles) or "none"
    message = f"{ctx.config_path} (revision {ctx.config.revision}; profiles: {ids})"
    return [Check("config.valid", G, "ok", message)]


def check_reserved_flags(ctx: Ctx) -> list[Check]:
    cid = "config.reserved_flags"
    if ctx.claude is None:
        return [Check(cid, G, "warn", "skipped: claude not found")]
    ran = ctx.env.run([ctx.claude, "--help"], None, ctx.env.subprocess_timeout)
    options = parse_help_options(ran.stdout) if ran.rc == 0 else set()
    if not options:
        detail = ran.stderr.strip()[:200] or f"exit {ran.rc}"
        return [Check(cid, G, "warn", f"could not read `claude --help` ({detail})")]
    flags = {p.flag: p.id for p in ctx.config.profiles} if ctx.config else {}
    collisions = sorted(f for f in flags if f in options)
    if collisions:
        first = collisions[0]
        return [
            Check(
                cid,
                G,
                "fail",
                "profile flag(s) collide with claude options: "
                + ", ".join(f"--{f} ({flags[f]})" for f in collisions),
                f"ccs profile set {flags[first]} flag=<new-flag>",
            )
        ]
    new = sorted(options - CLAUDE_LONG_OPTIONS)
    if new:
        return [
            Check(
                cid,
                G,
                "warn",
                "claude has options missing from ccs's reserved list: "
                + ", ".join(f"--{o}" for o in new),
                "upgrade ccs (CLAUDE_LONG_OPTIONS in ccs/config/validate.py), "
                "and avoid these names as profile flags",
            )
        ]
    return [Check(cid, G, "ok", f"{len(options)} claude options; no profile flag collides")]


def check_daemon_installed(ctx: Ctx) -> list[Check]:
    plist = ctx.env.launchd.plist
    if plist.exists():
        return [Check("daemon.installed", G, "ok", str(plist))]
    return [Check("daemon.installed", G, "fail", "LaunchAgent not installed", "ccs daemon install")]


def check_daemon_running(ctx: Ctx) -> list[Check]:
    cid = "daemon.running"
    launchd = ctx.env.launchd
    printed = launchd.print_service()
    if printed.rc == 0:
        from ccs.daemon.launchd import parse_print

        info = parse_print(printed.stdout)
        pid = info.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            return [Check(cid, G, "ok", f"running under launchd (pid {pid})")]
        state = info.get("state") or "unknown"
        return [
            Check(
                cid,
                G,
                "fail",
                f"loaded but not running (state {state})",
                "ccs daemon logs; ccs daemon restart",
            )
        ]
    probe = ctx.env.socket_probe()
    if probe is not None:
        pid = probe.get("daemon_pid")
        return [Check(cid, G, "ok", f"running outside launchd (pid {pid})")]
    if launchd.is_installed():
        return [Check(cid, G, "fail", "installed but not running", "ccs daemon start")]
    return [Check(cid, G, "fail", "not running (not installed)", "ccs daemon install")]


def check_daemon_socket(ctx: Ctx) -> list[Check]:
    cid = "daemon.socket"
    sock = paths.daemon_sock()
    probe = ctx.env.socket_probe()
    if probe is None or not probe.get("responsive"):
        fix = "ccs daemon restart" if ctx.env.launchd.is_installed() else "ccs daemon install"
        return [Check(cid, G, "fail", f"no response on {sock}", fix)]
    latency = probe.get("latency_ms")
    version = probe.get("version")
    shown = f"{latency:.0f} ms" if isinstance(latency, (int, float)) else "? ms"
    if isinstance(latency, (int, float)) and latency > SOCKET_LATENCY_MAX_MS:
        return [
            Check(cid, G, "warn", f"slow response ({shown})", "ccs daemon logs; ccs daemon restart")
        ]
    if isinstance(version, str) and version != __version__:
        return [
            Check(
                cid,
                G,
                "warn",
                f"daemon runs ccs {version}, this CLI is {__version__}",
                "ccs daemon restart",
            )
        ]
    return [Check(cid, G, "ok", f"responsive in {shown} (ccs {version})")]


def check_notifications(ctx: Ctx) -> list[Check]:
    cid = "notifications.route"
    status = ctx.env.daemon_status()
    if status is None:
        return [Check(cid, G, "warn", "unknown: daemon not reachable", "ccs daemon start")]
    daemon = status.get("daemon") if isinstance(status.get("daemon"), dict) else {}
    connected = daemon.get("app_connected") if isinstance(daemon, dict) else None
    if connected is True:
        return [Check(cid, G, "ok", "menu bar app connected (native notifications)")]
    if connected is None:
        return [
            Check(
                cid,
                G,
                "warn",
                "the daemon doesn't report app connections (older daemon)",
                "ccs daemon restart",
            )
        ]
    return [
        Check(
            cid,
            G,
            "warn",
            "no menu bar app connected: notifications use the osascript fallback",
            'open -a "CC Supervisor"',
        )
    ]


def check_app(ctx: Ctx) -> list[Check]:
    app = ctx.env.app_path
    if app.is_dir():
        return [Check("app.installed", G, "ok", str(app))]
    return [Check("app.installed", G, "warn", f"not installed at {app}", "make app")]


def check_state_dir(ctx: Ctx) -> list[Check]:
    cid = "state.dir"
    state = paths.state_dir()
    default = Path(os.path.expanduser(DEFAULT_STATE_DIR))
    if os.path.realpath(state) == os.path.realpath(default):
        return [Check(cid, G, "ok", str(state))]
    override = os.environ.get("CCS_STATE_DIR", "")
    var = "CCS_STATE_DIR" if override and os.path.isabs(override) else "XDG_STATE_HOME"
    return [
        Check(
            cid,
            G,
            "warn",
            f"{state} (from ${var}) is not the default {default}: "
            "widgets only read the default dir",
            f"unset {var}, then: ccs daemon install",
        )
    ]


def check_widget_snapshot(ctx: Ctx) -> list[Check]:
    cid = "widget.snapshot"
    path = paths.widget_snapshot()
    doc = read_json(path)
    if doc is None:
        return [Check(cid, G, "warn", f"no widget snapshot at {path}", "ccs daemon start")]
    age = _since(ctx, doc.get("generated_at"))
    if age is None:
        return [Check(cid, G, "warn", "snapshot has no generated_at", "ccs daemon restart")]
    if age > SNAPSHOT_FRESHNESS_S:
        return [
            Check(
                cid,
                G,
                "warn",
                f"stale: generated {_age(age)} ago (widgets show 'Supervisor offline')",
                "ccs daemon restart",
            )
        ]
    return [Check(cid, G, "ok", f"updated {_age(age)} ago")]


# ---------------------------------------------------------------- profile checks


def check_config_dir(ctx: Ctx, profile: Profile) -> list[Check]:
    scope = f"profile:{profile.id}"
    path = Path(profile.config_dir_env)
    if path.is_dir():
        return [Check("config_dir.exists", scope, "ok", str(path))]
    return [
        Check(
            "config_dir.exists",
            scope,
            "fail",
            f"{path} does not exist",
            f"mkdir -p {path}  (or: ccs profile set {profile.id} config_dir=<dir>)",
        )
    ]


def check_auth(ctx: Ctx, profile: Profile) -> list[Check]:
    scope = f"profile:{profile.id}"
    out = [
        Check(
            "auth.keychain_service",
            scope,
            "ok",
            auth.keychain_service_name(profile.config_dir),
        )
    ]
    if ctx.claude is None:
        out.insert(0, Check("auth.status", scope, "warn", "skipped: claude not found"))
        return out
    status = ctx.env.auth_status(ctx.claude, profile, ctx.env.subprocess_timeout)
    if status is None or status.error:
        detail = status.error if status is not None and status.error else "claude failed"
        out.insert(
            0,
            Check(
                "auth.status",
                scope,
                "warn",
                f"could not read auth status ({detail})",
                f"ccs auth status --profile {profile.id}",
            ),
        )
    elif status.logged_in:
        parts = [p for p in (status.auth_method, status.subscription_type) if p]
        detail = f" ({', '.join(parts)})" if parts else ""
        out.insert(0, Check("auth.status", scope, "ok", f"signed in{detail}"))
    else:
        out.insert(
            0,
            Check(
                "auth.status",
                scope,
                "warn",
                "not signed in",
                f"ccs auth login --profile {profile.id}",
            ),
        )
    return out


def check_usage(ctx: Ctx, profile: Profile) -> list[Check]:
    scope = f"profile:{profile.id}"
    refresh = f"ccs usage --refresh --profile {profile.id}"
    doc = read_json(paths.usage_file(profile.id))
    if doc is None:
        return [
            Check(
                "usage.last_poll",
                scope,
                "warn",
                "no usage data yet",
                "ccs daemon start  (the first poll runs right after start)",
            )
        ]
    out: list[Check] = []
    polled = _since(ctx, doc.get("polled_at") or doc.get("fetched_at"))
    fetched = _since(ctx, doc.get("fetched_at"))
    error = doc.get("error") if isinstance(doc.get("error"), str) else None
    if polled is None or polled > POLL_LIVENESS_S:
        shown = "never" if polled is None else f"{_age(polled)} ago"
        out.append(
            Check(
                "usage.last_poll",
                scope,
                "warn",
                f"last poll attempt {shown}: the daemon is not polling",
                "ccs daemon restart",
            )
        )
    elif fetched is None or fetched > DATA_FRESHNESS_S:
        shown = "never" if fetched is None else f"{_age(fetched)} ago"
        why = f" ({error})" if error else ""
        out.append(
            Check(
                "usage.last_poll",
                scope,
                "warn",
                f"last successful poll {shown}: polls are failing{why}",
                refresh,
            )
        )
    else:
        out.append(
            Check(
                "usage.last_poll",
                scope,
                "ok",
                f"polled {_age(polled)} ago; data from {_age(fetched)} ago",
            )
        )
    status = doc.get("status")
    sub = doc.get("subscription_type")
    if status == "ok":
        out.append(Check("usage.status", scope, "ok", f"ok ({sub})" if sub else "ok"))
    elif status == "needs_sign_in":
        out.append(
            Check(
                "usage.status",
                scope,
                "warn",
                "sign-in required",
                f"ccs auth login --profile {profile.id}",
            )
        )
    elif status == "no_subscription":
        out.append(
            Check(
                "usage.status",
                scope,
                "warn",
                "no Claude subscription on this login: plan limits are unavailable",
                f"ccs auth status --profile {profile.id}",
            )
        )
    elif status == "source_error":
        out.append(
            Check(
                "usage.status",
                scope,
                "fail",
                f"usage source error: {error or 'unknown'}",
                f"{refresh}  (get_usage is experimental: check `claude --version`)",
            )
        )
    elif status == "stale":
        out.append(Check("usage.status", scope, "warn", "data is stale", refresh))
    else:
        out.append(Check("usage.status", scope, "warn", f"unknown status {status!r}", refresh))
    return out


def check_statusline(ctx: Ctx, profile: Profile) -> list[Check]:
    scope = f"profile:{profile.id}"
    generate = f"ccs statusline generate --profile {profile.id}"
    out: list[Check] = []
    if not profile.statusline.enabled:
        out.append(Check("statusline.script", scope, "ok", "statusline disabled for this profile"))
        if profile.supervisor.enabled and not profile.limits.model_scoped.warn_only:
            out.append(
                Check(
                    "statusline.model_scoped",
                    scope,
                    "warn",
                    "model-scoped pauses can't work: a session's model is only known from "
                    "its statusline, which is disabled",
                    f"ccs profile set {profile.id} statusline.enabled=true  "
                    "(or: limits.model_scoped.warn_only=true)",
                )
            )
        return out
    script = template.script_path(profile)
    try:
        text: str | None = script.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError) as exc:
        return [Check("statusline.script", scope, "warn", f"unreadable: {exc}", generate)]
    if text is None:
        flag = f"ccs --{profile.flag}"
        out.append(
            Check(
                "statusline.script",
                scope,
                "ok",
                f"not generated yet (created on the first `{flag}` run)",
            )
        )
        out.append(Check("statusline.interpreter", scope, "ok", "n/a (no script yet)"))
    else:
        header = template.parse_header(text)
        current_hash = template.sources_hash(template.embedded_sources())
        if header is None:
            out.append(
                Check(
                    "statusline.script",
                    scope,
                    "warn",
                    f"{script} is not a generated script",
                    generate,
                )
            )
        elif (
            header.get("generator") != str(template.GENERATOR_VERSION)
            or header.get("sources") != current_hash
        ):
            out.append(
                Check(
                    "statusline.script",
                    scope,
                    "warn",
                    f"outdated (generator {header.get('generator')}, sources "
                    f"{header.get('sources')}; current "
                    f"{template.GENERATOR_VERSION}/{current_hash})",
                    generate,
                )
            )
        elif ctx.config is not None and not template.script_is_current(profile, ctx.config):
            out.append(
                Check(
                    "statusline.script",
                    scope,
                    "warn",
                    "profile settings or interpreter changed since it was generated",
                    generate,
                )
            )
        else:
            out.append(
                Check(
                    "statusline.script",
                    scope,
                    "ok",
                    f"current (generator {template.GENERATOR_VERSION})",
                )
            )
        python = template.parse_shebang(text)
        if python and os.path.isfile(python) and os.access(python, os.X_OK):
            out.append(Check("statusline.interpreter", scope, "ok", python))
        else:
            out.append(
                Check(
                    "statusline.interpreter",
                    scope,
                    "fail",
                    f"interpreter {python or '(none)'} is missing",
                    generate,
                )
            )
    out.append(_check_applied(profile, script))
    return out


def _check_applied(profile: Profile, script: Path) -> Check:
    scope = f"profile:{profile.id}"
    cid = "statusline.applied"
    apply = f"ccs statusline apply --profile {profile.id}"
    path = statusline_apply.settings_path(profile)
    try:
        settings = statusline_apply.read_settings(path)
    except statusline_apply.ApplyError as exc:
        return Check(cid, scope, "warn", str(exc), f"fix {path} by hand")
    value = settings.get("statusLine") if settings is not None else None
    if value is None:
        return Check(
            cid,
            scope,
            "ok",
            f"not applied to settings.json: `ccs --{profile.flag}` injects it; "
            f"`{apply}` makes plain claude show it too",
        )
    if statusline_apply.is_ours(value, script):
        if not script.is_file():
            return Check(
                cid,
                scope,
                "fail",
                f"applied, but {script} is missing: plain claude shows no statusline",
                f"ccs statusline generate --profile {profile.id}",
            )
        if value.get("command") == template.statusline_command(script):
            return Check(cid, scope, "ok", "applied")
        return Check(cid, scope, "warn", "applied with an outdated command", apply)
    return Check(
        cid,
        scope,
        "ok",
        f"settings.json has a different statusLine; `ccs --{profile.flag}` still shows its own",
    )


def check_supervisor(ctx: Ctx, profile: Profile) -> list[Check]:
    scope = f"profile:{profile.id}"
    cid = "supervisor.state"
    path = paths.supervisor_file(profile.id)
    if not path.exists():
        return [Check(cid, scope, "ok", "no pauses recorded")]
    doc = read_json(path)
    holds = doc.get("holds") if doc is not None else None
    if doc is None or doc.get("schema") != 1 or not isinstance(holds, list):
        return [
            Check(
                cid,
                scope,
                "warn",
                f"{path} does not match the supervisor-state schema",
                "ccs daemon restart",
            )
        ]
    overdue: list[str] = []
    for hold in holds:
        if not isinstance(hold, dict):
            continue
        age = _since(ctx, hold.get("resets_at"))
        if age is not None and age > HOLD_OVERDUE_S:
            overdue.append(f"{hold.get('id')} (reset {_age(age)} ago)")
    if overdue:
        return [
            Check(
                cid,
                scope,
                "warn",
                "pauses that should have cleared: " + ", ".join(overdue),
                f"ccs daemon restart  (or: ccs resume --profile {profile.id})",
            )
        ]
    if holds:
        ids = ", ".join(str(h.get("id")) for h in holds if isinstance(h, dict))
        return [Check(cid, scope, "ok", f"paused: {ids}")]
    return [Check(cid, scope, "ok", "no active pauses")]


GLOBAL_CHECKS: tuple[tuple[str, Callable[[Ctx], list[Check]]], ...] = (
    ("python.version", check_python),
    ("ccs.version", check_ccs),
    ("claude.found", check_claude_found),
    ("claude.version", check_claude_version),
    ("config.valid", check_config),
    ("config.reserved_flags", check_reserved_flags),
    ("daemon.installed", check_daemon_installed),
    ("daemon.running", check_daemon_running),
    ("daemon.socket", check_daemon_socket),
    ("notifications.route", check_notifications),
    ("app.installed", check_app),
    ("state.dir", check_state_dir),
    ("widget.snapshot", check_widget_snapshot),
)

PROFILE_CHECKS: tuple[tuple[str, Callable[[Ctx, Profile], list[Check]]], ...] = (
    ("config_dir.exists", check_config_dir),
    ("auth.status", check_auth),
    ("usage.last_poll", check_usage),
    ("statusline.script", check_statusline),
    ("supervisor.state", check_supervisor),
)


# ---------------------------------------------------------------- runner


class _Job:
    def __init__(self, check_id: str, scope: str, fn: Callable[[], list[Check]]) -> None:
        self.check_id = check_id
        self.scope = scope
        self.fn = fn
        self.result: list[Check] | None = None
        self.thread = threading.Thread(target=self._run, name=f"doctor:{check_id}", daemon=True)

    def _run(self) -> None:
        try:
            self.result = self.fn()
        except Exception as exc:  # a check must never take the doctor down
            self.result = [
                Check(self.check_id, self.scope, "fail", f"check crashed: {type(exc).__name__}")
            ]


def run_checks(ctx: Ctx) -> list[Check]:
    """Run every check in parallel; a check past its deadline becomes a `fail`."""
    jobs: list[_Job] = []
    for check_id, gfn in GLOBAL_CHECKS:
        jobs.append(_Job(check_id, G, functools.partial(gfn, ctx)))
    for profile in ctx.config.profiles if ctx.config else ():
        for check_id, pfn in PROFILE_CHECKS:
            scope = f"profile:{profile.id}"
            jobs.append(_Job(check_id, scope, functools.partial(pfn, ctx, profile)))
    for job in jobs:
        job.thread.start()
    deadline = time.monotonic() + ctx.env.check_timeout
    results: list[Check] = []
    for job in jobs:
        job.thread.join(max(0.0, deadline - time.monotonic()))
        if job.result is None:
            results.append(
                Check(
                    job.check_id,
                    job.scope,
                    "fail",
                    f"timed out after {ctx.env.check_timeout:.0f}s",
                )
            )
        else:
            results.extend(job.result)
    return results


def summarize(checks: Sequence[Check]) -> dict[str, int]:
    summary = {"ok": 0, "warn": 0, "fail": 0}
    for check in checks:
        summary[check.status] += 1
    return summary


def to_json(checks: Sequence[Check]) -> dict[str, Any]:
    """`{"checks":[…],"summary":{"ok","warn","fail"}}` (the app's `DoctorResult`)."""
    return {"checks": [c.to_dict() for c in checks], "summary": summarize(checks)}


_MARK = {"ok": "✓", "warn": "!", "fail": "✗"}


def render(checks: Sequence[Check], config: Config | None) -> str:
    """Human output grouped by scope, with a fix line under every non-ok check."""
    names = (
        {f"profile:{p.id}": f"{p.emoji} {p.name} ({p.id})".strip() for p in config.profiles}
        if config
        else {}
    )
    lines: list[str] = []
    scopes: list[str] = []
    for check in checks:
        if check.scope not in scopes:
            scopes.append(check.scope)
    for scope in scopes:
        title = "Global" if scope == G else f"Profile {names.get(scope, scope.split(':', 1)[-1])}"
        lines.append(title)
        for check in (c for c in checks if c.scope == scope):
            lines.append(f"  {_MARK[check.status]} {check.id}: {check.message}")
            if check.fix and check.status != "ok":
                lines.append(f"      fix: {check.fix}")
        lines.append("")
    s = summarize(checks)
    lines.append(f"{s['ok']} ok · {s['warn']} warnings · {s['fail']} failed")
    return "\n".join(lines)


# The environment `ccs doctor` uses; tests replace it to stub launchctl, sockets and time.
ENV_FACTORY: Callable[[], Env] = Env


def run(env: Env | None = None) -> tuple[list[Check], Config | None]:
    ctx = build_ctx(env if env is not None else ENV_FACTORY())
    return run_checks(ctx), ctx.config


# ---------------------------------------------------------------- CLI


def cmd_doctor(args: argparse.Namespace) -> int:
    checks, config = run()
    if args.json:
        emit_json(to_json(checks))
    else:
        print(render(checks, config))
    return EXIT_ERROR if any(c.status == "fail" for c in checks) else EXIT_OK


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("doctor", help="diagnose the installation (read-only), with fix hints")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_doctor)


__all__ = [
    "Check",
    "Ctx",
    "Env",
    "Ran",
    "build_ctx",
    "cmd_doctor",
    "parse_help_options",
    "register",
    "render",
    "run",
    "run_checks",
    "summarize",
    "to_json",
]
