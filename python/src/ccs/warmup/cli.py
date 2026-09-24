"""`ccs warmup (--profile <id> | --all) [--trigger …] [--force] [--json]` (ADR-0010/0017).

Needs the daemon: it owns rules and execution. Decisions come back right away; started runs
report their outcome as `warmup.*` events (`ccs events --follow`).
"""

from __future__ import annotations

import argparse
from typing import Any

from ccs.clock import SystemClock, local_tz
from ccs.config import store
from ccs.config.models import Config
from ccs.daemon.client import DaemonClient, DaemonUnavailable
from ccs.output import EXIT_OK, EXIT_USAGE, emit_json, fail
from ccs.timefmt import format_reset_absolute
from ccs.usage.model import parse_time
from ccs.warmup import rules

DAEMON_HINT = "daemon not running · start it with: ccs daemon start"
REPLY_TIMEOUT_S = 10.0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("warmup", help="start a session window early (warm-up)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--profile", metavar="<id>", help="warm up this profile")
    target.add_argument("--all", action="store_true", help="evaluate every profile")
    p.add_argument(
        "--trigger",
        choices=rules.TRIGGERS,
        default=rules.MANUAL,
        help="trigger to evaluate (default: manual)",
    )
    p.add_argument(
        "--force", action="store_true", help="ignore window/busy/cooldown/hours/weekly rules"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_warmup)


def describe(cfg: Config, result: dict[str, Any]) -> str:
    """One human line per profile result."""
    pid = str(result.get("profile_id"))
    profile = cfg.profile(pid)
    head = f"{profile.emoji} {pid}" if profile is not None else pid
    if result.get("decision") == "started":
        return f"{head}: started (window will be confirmed; see ccs events --follow)"
    reason = str(result.get("reason") or "skipped")
    resets = parse_time(result.get("resets_at"))
    if reason == rules.WINDOW_ACTIVE and resets is not None:
        clock = SystemClock()
        reason += f", resets {format_reset_absolute(resets, clock.now(), local_tz())}"
    return f"{head}: skipped ({reason})"


def cmd_warmup(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    try:
        cfg = store.ensure_config()
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    if args.profile and cfg.profile(args.profile) is None:
        return fail(as_json, f"unknown profile '{args.profile}'", code=EXIT_USAGE)
    fields: dict[str, Any] = {"trigger": args.trigger, "force": bool(args.force)}
    if args.all:
        fields["all"] = True
    else:
        fields["profile_id"] = args.profile
    try:
        with DaemonClient(timeout=REPLY_TIMEOUT_S) as client:
            if not client.hello().get("ok"):
                return fail(as_json, DAEMON_HINT)
            reply = client.request("warmup", **fields)
    except DaemonUnavailable:
        return fail(as_json, DAEMON_HINT)
    reply.pop("id", None)
    reply.pop("proto", None)
    if not reply.get("ok"):
        error = str(reply.get("error") or "warm-up request failed")
        detail = reply.get("detail")
        return fail(as_json, f"{error}: {detail}" if detail else error)
    if as_json:
        emit_json(reply)
        return EXIT_OK
    for result in reply.get("results") or []:
        if isinstance(result, dict):
            print(describe(cfg, result))
    return EXIT_OK
