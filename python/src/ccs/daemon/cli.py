"""`ccs daemon …`, `ccs status`, `ccs events` (ADR-0017)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ccs import fsio, paths
from ccs.clock import SystemClock, local_tz
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.daemon import launchd
from ccs.daemon.client import DaemonClient, DaemonUnavailable
from ccs.events import read_tail
from ccs.output import EXIT_ERROR, EXIT_OK, EXIT_USAGE, emit_json, eprint, fail
from ccs.timefmt import format_relative
from ccs.usage.model import UsageSnapshot, parse_time, to_utc_seconds

LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
LOG_TAIL = 200
EVENTS_TAIL = 50

Handler = Callable[[argparse.Namespace], int]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    d = sub.add_parser("daemon", help="the background supervisor (LaunchAgent)")
    dsub = d.add_subparsers(dest="daemon_command", metavar="<action>")
    actions: list[tuple[str, str, Handler]] = [
        ("install", "install and start the LaunchAgent", cmd_install),
        ("uninstall", "stop and remove the LaunchAgent", cmd_uninstall),
        ("start", "start the installed daemon", cmd_start),
        ("stop", "stop the daemon (keeps it installed)", cmd_stop),
        ("restart", "restart the daemon", cmd_restart),
        ("status", "daemon health", cmd_daemon_status),
        ("run", "run in the foreground (what launchd executes)", cmd_run),
        ("logs", f"print the last {LOG_TAIL} daemon log lines", cmd_logs),
    ]
    for name, help_text, handler in actions:
        p = dsub.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if name == "run":
            p.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
            p.add_argument(launchd.LAUNCHD_FLAG, action="store_true", help=argparse.SUPPRESS)
        p.set_defaults(func=handler)
    d.set_defaults(func=lambda args: _usage(d))

    s = sub.add_parser("status", help="usage + supervisor state + daemon health")
    s.add_argument("--profile", metavar="<id>", help="only this profile")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_status)

    e = sub.add_parser("events", help="recent events (or follow them live)")
    e.add_argument("--follow", action="store_true", help="keep printing new events")
    e.add_argument("--test", action="store_true", help="send a test notification")
    e.add_argument("--json", action="store_true", help="one JSON event per line")
    e.set_defaults(func=cmd_events)


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return EXIT_USAGE


# ---------------------------------------------------------------- daemon run


def setup_logging(foreground: bool) -> None:
    """Rotating `logs/daemon.log` (5 MB x 3); also stderr in the foreground."""
    paths.ensure_state_layout()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        paths.daemon_log(), maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)
    if foreground:
        err = logging.StreamHandler(sys.stderr)
        err.setFormatter(fmt)
        root.addHandler(err)


def cmd_run(args: argparse.Namespace) -> int:
    from ccs.daemon.server import Daemon, DaemonAlreadyRunning

    setup_logging(bool(getattr(args, "foreground", False)) or sys.stderr.isatty())
    # under launchd, wait for a running daemon instead of exiting: `KeepAlive` would respawn
    # us every 10 s (ADR-0022); a foreground run just reports it
    under_launchd = bool(getattr(args, "launchd", False)) or launchd.launched_by_agent()
    try:
        asyncio.run(Daemon().run(wait_for_lock=under_launchd))
    except DaemonAlreadyRunning:
        print("daemon already running")
        return EXIT_OK
    except KeyboardInterrupt:
        pass
    return EXIT_OK


# ---------------------------------------------------------------- launchd actions


def _launchd_action(args: argparse.Namespace, action: str, fn: Callable[[], None]) -> int:
    as_json = bool(args.json)
    try:
        fn()
    except launchd.LaunchdError as exc:
        return fail(as_json, str(exc))
    if as_json:
        emit_json({"ok": True, "action": action, "label": launchd.LABEL})
    else:
        print(f"daemon {action}: ok")
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    ld = launchd.Launchd()
    code = _launchd_action(args, "installed", ld.install)
    if code == EXIT_OK and not args.json:
        print(f"plist: {ld.plist}")
    return code


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _launchd_action(args, "uninstalled", launchd.Launchd().uninstall)


def cmd_start(args: argparse.Namespace) -> int:
    return _launchd_action(args, "started", launchd.Launchd().start)


def cmd_stop(args: argparse.Namespace) -> int:
    return _launchd_action(args, "stopped", launchd.Launchd().stop)


def cmd_restart(args: argparse.Namespace) -> int:
    return _launchd_action(args, "restarted", launchd.Launchd().restart)


def _duration(seconds: int | None) -> str:
    """`2h 13m`, `42m`, `<1m` for an uptime in seconds."""
    if seconds is None:
        return "?"
    now = datetime.now(UTC)
    rel = format_relative(now + timedelta(seconds=seconds), now)
    return "<1m" if rel == "now" else rel.removeprefix("in ")


def cmd_daemon_status(args: argparse.Namespace) -> int:
    info = launchd.Launchd().status()
    if args.json:
        emit_json({"ok": True, **info})
        return EXIT_OK
    if info["responsive"]:
        print(
            f"daemon: running (pid {info.get('daemon_pid') or info.get('pid')}, "
            f"up {_duration(info.get('uptime_s'))}, {info.get('latency_ms')} ms)"
        )
    elif info["loaded"]:
        print("daemon: loaded but not responding · check: ccs daemon logs")
    elif info["installed"]:
        print("daemon: installed, not running · start with: ccs daemon start")
    else:
        print("daemon: not installed · install with: ccs daemon install")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    path = paths.daemon_log()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
    except FileNotFoundError:
        lines = []
    if args.json:
        emit_json({"ok": True, "path": str(path), "lines": lines})
    else:
        if not lines:
            eprint(f"no daemon log yet ({path})")
        for line in lines:
            print(line)
    return EXIT_OK


# ---------------------------------------------------------------- status


def _from_daemon(profile_id: str | None) -> dict[str, Any] | None:
    try:
        with DaemonClient(timeout=2.0) as client:
            if not client.hello().get("ok"):
                return None
            fields = {"profile_id": profile_id} if profile_id else {}
            reply = client.request("status", **fields)
    except DaemonUnavailable:
        return None
    return reply if reply.get("ok") else None


def _read_sessions(profile_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        files = sorted(paths.sessions_dir().glob("*.json"))
    except OSError:
        return out
    for path in files:
        rec = fsio.read_json(path)
        if rec is not None and rec.get("profile_id") == profile_id:
            out.append(rec)
    return out


def status_from_files(cfg: Config, profile_id: str | None) -> dict[str, Any]:
    """The `status` shape assembled from state files (daemon not responding)."""
    from ccs.usage.cli import local_view

    now = to_utc_seconds(SystemClock().now())
    profiles: list[dict[str, Any]] = []
    for p in cfg.profiles:
        if profile_id and p.id != profile_id:
            continue
        snap = local_view(p.id, now)
        sup = fsio.read_json(paths.supervisor_file(p.id))
        profiles.append(
            {
                "id": p.id,
                "usage": snap.to_dict() if snap is not None else None,
                "supervisor": sup if sup is not None else {"state": "normal", "holds": []},
                "sessions": _read_sessions(p.id),
                "other_sessions": None,
                "next_warmup_at": None,
            }
        )
    return {"daemon": {"responsive": False}, "profiles": profiles}


def _sessions_line(entry: dict[str, Any]) -> str:
    sessions = entry.get("sessions") or []
    paused = sum(
        1
        for r in sessions
        if isinstance(r, dict)
        and isinstance(r.get("supervision"), dict)
        and r["supervision"].get("state") == "paused"
    )
    text = f"sessions: {len(sessions)} supervised"
    if paused:
        text += f" ({paused} paused)"
    other = entry.get("other_sessions")
    if isinstance(other, dict):
        text += (
            f" · other: {other.get('interactive', 0)} interactive, "
            f"{other.get('background', 0)} background"
        )
    return text


def render_status(cfg: Config, payload: dict[str, Any]) -> str:
    from ccs.usage.cli import display_width, render_human

    now = to_utc_seconds(SystemClock().now())
    daemon = payload.get("daemon") or {}
    if daemon.get("responsive"):
        head = f"daemon: running (pid {daemon.get('pid')}, up {_duration(daemon.get('uptime_s'))})"
    else:
        head = "daemon: not running · start with: ccs daemon start (showing saved state)"
    blocks = [head]
    for entry in payload.get("profiles") or []:
        profile: Profile | None = cfg.profile(str(entry.get("id")))
        if profile is None:
            continue
        snap = UsageSnapshot.from_dict(entry.get("usage"))
        block = render_human([(profile, snap)], now, local_tz())
        indent = display_width(f"{profile.emoji} {profile.name}".strip()) + 3
        blocks.append(block + "\n" + " " * indent + _sessions_line(entry))
    return "\n".join(blocks)


def cmd_status(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    try:
        cfg = store.ensure_config()
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    if args.profile and cfg.profile(args.profile) is None:
        return fail(as_json, f"unknown profile '{args.profile}'", code=EXIT_USAGE)
    payload = _from_daemon(args.profile)
    if payload is None:
        payload = status_from_files(cfg, args.profile)
    payload.pop("id", None)
    payload.pop("proto", None)
    payload["ok"] = True
    if as_json:
        emit_json(payload)
    else:
        print(render_status(cfg, payload))
    return EXIT_OK


# ---------------------------------------------------------------- events


def format_event(event: dict[str, Any]) -> str:
    """`HH:MM:SS  type  profile  title — body` (or the key when there is no title)."""
    ts = parse_time(event.get("ts"))
    when = ts.astimezone(local_tz()).strftime("%H:%M:%S") if ts else "--:--:--"
    raw = event.get("data")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    title = data.get("title") if isinstance(data.get("title"), str) else ""
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    detail = f"{title} — {body}" if title and body else title or str(event.get("key") or "")
    profile = event.get("profile_id") or "-"
    return f"{when}  {event.get('type')}  {profile}  {detail}".rstrip()


def _print_event(event: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        print(format_event(event), flush=True)


def _log_state(path: Path) -> tuple[tuple[int, int] | None, int]:
    """`((dev, inode), size)` of the event log, or `(None, 0)` while it doesn't exist."""
    try:
        st = path.stat()
    except OSError:
        return None, 0
    return (st.st_dev, st.st_ino), st.st_size


def _follow_file(as_json: bool) -> int:
    path = paths.events_file()
    ident, pos = _log_state(path)
    buf = ""
    try:
        while True:
            current, size = _log_state(path)
            if current != ident or size < pos:
                # rotated (a new file) or truncated: read it from the start, and drop the
                # partial line read from the old file, or it would swallow the first record
                ident, pos, buf = current, 0, ""
            if size > pos:
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        buf += fh.read()
                        pos = fh.tell()
                except OSError:  # rotated between stat and open: next round
                    time.sleep(1.0)
                    continue
                *lines, buf = buf.split("\n")
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        _print_event(event, as_json)
            time.sleep(1.0)
    except KeyboardInterrupt:
        return EXIT_OK


def _send_test_notification(as_json: bool) -> int:
    """`ccs events --test`: the daemon emits a `notify.test` event (ADR-0015)."""
    try:
        client = DaemonClient(timeout=5.0)
        client.connect()
        try:
            client.hello()
            reply = client.request("notify_test")
        finally:
            client.close()
    except DaemonUnavailable:
        msg = "daemon not running: start it with `ccs daemon start`"
        if as_json:
            emit_json({"ok": False, "error": msg})
        else:
            eprint(msg)
        return EXIT_ERROR
    ok = reply.get("ok") is True
    if as_json:
        emit_json({"ok": ok, "via": reply.get("via"), "app_connected": reply.get("app_connected")})
    elif ok:
        via = (
            "the menu bar app"
            if reply.get("via") == "app"
            else "a script notification (menu bar app not running)"
        )
        print(f"test notification sent via {via}")
    else:
        eprint(f"test notification failed: {reply.get('error', 'unknown error')}")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_events(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    if args.test:
        return _send_test_notification(as_json)
    if not args.follow:
        events = read_tail(limit=EVENTS_TAIL)
        if as_json:
            emit_json({"ok": True, "events": events})
        else:
            for event in events:
                print(format_event(event))
        return EXIT_OK
    try:
        client = DaemonClient(timeout=2.0)
        client.connect()
        if not client.hello().get("ok") or not client.subscribe(["events"]).get("ok"):
            raise DaemonUnavailable("subscribe failed")
    except DaemonUnavailable:
        eprint("daemon not running: following the event log file")
        return _follow_file(as_json)
    try:
        for msg in client.iter_pushes():
            pushed = msg.get("event")
            if isinstance(pushed, dict):
                _print_event(pushed, as_json)
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        client.close()
    eprint("daemon closed the connection")
    return EXIT_ERROR
