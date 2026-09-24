"""`ccs pause`, `ccs resume`, `ccs sessions` (ADR-0007/0008/0017).

`pause` / `resume` need the daemon (it owns the holds). `sessions` asks the daemon's `status`
op and falls back to the state files when it isn't running.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ccs.clock import SystemClock, local_tz
from ccs.config import store
from ccs.config.models import Config
from ccs.daemon.client import DaemonClient, DaemonUnavailable
from ccs.output import EXIT_OK, EXIT_USAGE, emit_json, fail
from ccs.timefmt import format_reset_combined
from ccs.usage.model import parse_time

NOT_RUNNING = "supervisor not running · start it with: ccs daemon start"
REPLY_TIMEOUT_S = 10.0
SHORT_ID = 8

ERRORS = {
    "unknown_session": "no supervised session with that id (see: ccs sessions)",
    "unknown_profile": "unknown profile",
    "supervisor_disabled": "supervision is off for this profile (supervisor.enabled)",
}


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for name, help_text in (
        ("pause", "pause a profile's ccs sessions (or one session) now"),
        ("resume", "resume paused ccs sessions now"),
    ):
        p = sub.add_parser(name, help=help_text)
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--profile", metavar="<id>", help="every ccs session of the profile")
        target.add_argument(
            "--session", metavar="<wrapper_id>", help="one session (id or prefix, ccs sessions)"
        )
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.set_defaults(func=cmd_control, op=name)

    s = sub.add_parser("sessions", help="supervised ccs sessions and other Claude sessions")
    s.add_argument("--profile", metavar="<id>", help="only this profile")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_sessions)


# ---------------------------------------------------------------- daemon access


def _status(client: DaemonClient, profile_id: str | None = None) -> dict[str, Any] | None:
    fields = {"profile_id": profile_id} if profile_id else {}
    reply = client.request("status", **fields)
    return reply if reply.get("ok") else None


def resolve_session(status: dict[str, Any] | None, wanted: str) -> str | None:
    """A full wrapper id from an exact id or a unique prefix (`None` if unknown/ambiguous)."""
    ids: list[str] = []
    for entry in (status or {}).get("profiles") or []:
        for rec in entry.get("sessions") or [] if isinstance(entry, dict) else []:
            wid = rec.get("wrapper_id") if isinstance(rec, dict) else None
            if isinstance(wid, str):
                ids.append(wid)
    if wanted in ids:
        return wanted
    matches = [w for w in ids if w.startswith(wanted)]
    return matches[0] if len(matches) == 1 else None


def describe_control(cfg: Config, op: str, reply: dict[str, Any]) -> str:
    pid = str(reply.get("profile_id") or "")
    profile = cfg.profile(pid)
    head = f"{profile.emoji} {profile.name}" if profile is not None else pid
    wid = reply.get("wrapper_id")
    target = f"session {str(wid)[:SHORT_ID]} ({head})" if isinstance(wid, str) else head
    if op == "pause":
        n = int(reply.get("sessions_paused") or 0)
        if not reply.get("created"):
            return f"{target}: already paused manually"
        noun = "session" if n == 1 else "sessions"
        return f"{target}: paused ({n} {noun}) · resume with: ccs resume --profile {pid}"
    n = int(reply.get("sessions_resumed") or 0)
    cleared = reply.get("cleared") or []
    if not n and not cleared:
        return f"{target}: nothing was paused"
    noun = "session" if n == 1 else "sessions"
    return f"{target}: resumed ({n} {noun})"


def cmd_control(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    op = str(args.op)
    try:
        cfg = store.ensure_config()
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    if args.profile and cfg.profile(args.profile) is None:
        return fail(as_json, f"unknown profile '{args.profile}'", code=EXIT_USAGE)
    try:
        with DaemonClient(timeout=REPLY_TIMEOUT_S) as client:
            if not client.hello().get("ok"):
                return fail(as_json, NOT_RUNNING)
            fields: dict[str, Any]
            if args.session:
                wid = resolve_session(_status(client), str(args.session))
                if wid is None:
                    return fail(as_json, ERRORS["unknown_session"], code=EXIT_USAGE)
                fields = {"wrapper_id": wid}
            else:
                fields = {"profile_id": args.profile}
            reply = client.request(op, **fields)
    except DaemonUnavailable:
        return fail(as_json, NOT_RUNNING)
    reply.pop("id", None)
    reply.pop("proto", None)
    if not reply.get("ok"):
        error = str(reply.get("error") or f"{op} failed")
        return fail(as_json, ERRORS.get(error, error))
    if as_json:
        emit_json(reply)
    else:
        print(describe_control(cfg, op, reply))
    return EXIT_OK


# ---------------------------------------------------------------- sessions


def _session_row(pid: str, rec: dict[str, Any]) -> dict[str, Any]:
    sup = rec.get("supervision") if isinstance(rec.get("supervision"), dict) else {}
    assert isinstance(sup, dict)
    return {
        "wrapper_id": rec.get("wrapper_id"),
        "profile_id": pid,
        "cwd": rec.get("cwd"),
        "model_id": rec.get("model_id"),
        "activity": rec.get("activity") or "unknown",
        "session_id": rec.get("session_id"),
        "started_at": rec.get("started_at"),
        "state": sup.get("state") or "running",
        "holds": sup.get("holds") or [],
        "resume_at": sup.get("resume_at"),
    }


def collect_sessions(status: dict[str, Any], profile_id: str | None) -> dict[str, Any]:
    """`{sessions: [...], other_sessions: {profile: counts}}` from a `status` payload."""
    rows: list[dict[str, Any]] = []
    others: dict[str, Any] = {}
    for entry in status.get("profiles") or []:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("id"))
        if profile_id and pid != profile_id:
            continue
        for rec in entry.get("sessions") or []:
            if isinstance(rec, dict):
                rows.append(_session_row(pid, rec))
        others[pid] = entry.get("other_sessions")
    return {"sessions": rows, "other_sessions": others}


def _home_path(cwd: Any) -> str:
    if not isinstance(cwd, str) or not cwd:
        return "-"
    home = str(Path.home())
    return "~" + cwd[len(home) :] if cwd == home or cwd.startswith(home + "/") else cwd


def render_sessions(cfg: Config, data: dict[str, Any]) -> str:
    now = SystemClock().now()
    lines: list[str] = []
    for row in data["sessions"]:
        profile = cfg.profile(str(row["profile_id"]))
        head = f"{profile.emoji} {profile.id}" if profile is not None else str(row["profile_id"])
        state = str(row["state"])
        resume = parse_time(row.get("resume_at"))
        if state == "paused":
            state = (
                f"⏸ paused → resumes {format_reset_combined(resume, now, local_tz())}"
                if resume is not None
                else "⏸ paused (manual)"
            )
        elif state == "overridden":
            state = "override"
        wid = str(row.get("wrapper_id") or "")[:SHORT_ID]
        model = row.get("model_id") or "-"
        lines.append(
            f"{wid}  {head}  {_home_path(row.get('cwd'))}  {model}  {row['activity']}  {state}"
        )
    if not lines:
        lines.append("no supervised ccs sessions")
    for pid, counts in data["other_sessions"].items():
        if isinstance(counts, dict):
            lines.append(
                f"{pid}: other sessions {counts.get('interactive', 0)} interactive, "
                f"{counts.get('background', 0)} background"
            )
    return "\n".join(lines)


def cmd_sessions(args: argparse.Namespace) -> int:
    from ccs.daemon.cli import status_from_files

    as_json = bool(args.json)
    try:
        cfg = store.ensure_config()
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    if args.profile and cfg.profile(args.profile) is None:
        return fail(as_json, f"unknown profile '{args.profile}'", code=EXIT_USAGE)
    status: dict[str, Any] | None = None
    try:
        with DaemonClient(timeout=REPLY_TIMEOUT_S) as client:
            if client.hello().get("ok"):
                status = _status(client, args.profile)
    except DaemonUnavailable:
        status = None
    responsive = status is not None
    if status is None:
        status = status_from_files(cfg, args.profile)
    data = collect_sessions(status, args.profile)
    if as_json:
        emit_json({"ok": True, "daemon_responsive": responsive, **data})
    else:
        if not responsive:
            print("daemon: not running (showing saved state)")
        print(render_sessions(cfg, data))
    return EXIT_OK
