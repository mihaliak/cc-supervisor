"""`ccs usage [--profile <id>] [--refresh] [--json]` (ADR-0017)."""

from __future__ import annotations

import argparse
import asyncio
import time
import unicodedata
from datetime import datetime, tzinfo
from typing import Any

from ccs.clock import SystemClock, local_tz
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.daemon.client import DaemonClient, DaemonUnavailable
from ccs.output import EXIT_OK, EXIT_USAGE, emit_json, fail
from ccs.snapshot import format_money
from ccs.timefmt import format_relative, format_reset_combined
from ccs.usage.merge import apply_staleness, merge
from ccs.usage.model import (
    STATUS_NEEDS_SIGN_IN,
    STATUS_NO_SUBSCRIPTION,
    STATUS_OK,
    STATUS_SOURCE_ERROR,
    STATUS_STALE,
    ExtraUsage,
    UsageSnapshot,
    to_utc_seconds,
)
from ccs.usage.source_claude import fetch_snapshot, read_live_reports, read_snapshot

SOURCE_FILE = "file"
SOURCE_DAEMON = "daemon"
SOURCE_DIRECT = "direct_probe"
REFRESH_WAIT_S = 20.0
NO_DATA_HINT = "run: ccs usage --refresh"
NO_RESET = "\u2013"  # en dash: window without a reset time


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("usage", help="usage numbers per profile")
    p.add_argument("--profile", metavar="<id>", help="only this profile")
    p.add_argument("--refresh", action="store_true", help="fetch fresh data now")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_usage)


# ---------------------------------------------------------------- data


def local_view(profile_id: str, now: datetime) -> UsageSnapshot | None:
    """`usage/<id>.json` merged with fresh live reports, staleness applied."""
    merged = merge(
        read_snapshot(profile_id), read_live_reports(profile_id), now=now, profile_id=profile_id
    )
    return apply_staleness(merged, now) if merged is not None else None


def _entry(profile_id: str, snap: UsageSnapshot | None, source: str) -> dict[str, Any]:
    if snap is None:
        return {"schema": 1, "profile_id": profile_id, "status": "no_data", "hint": NO_DATA_HINT}
    return {**snap.to_dict(), "source": source}


def _refresh_via_daemon(profiles: list[Profile], target: str | None) -> bool:
    """Ask a running daemon to poll, then wait (≤ 20 s) for each `polled_at` to move."""
    before = {p.id: _polled_at(p.id) for p in profiles}
    try:
        with DaemonClient(timeout=2.0) as client:
            if not client.hello().get("ok"):
                return False
            fields: dict[str, Any] = {"profile_id": target} if target else {}
            reply = client.request("refresh", **fields)
    except DaemonUnavailable:
        return False
    if not reply.get("ok"):
        return False
    deadline = time.monotonic() + REFRESH_WAIT_S
    pending = set(before)
    while pending and time.monotonic() < deadline:
        pending = {pid for pid in pending if _polled_at(pid) == before[pid]}
        if pending:
            time.sleep(0.25)
    return True


def _polled_at(profile_id: str) -> datetime | None:
    snap = read_snapshot(profile_id)
    return snap.polled_at if snap is not None else None


async def _direct(cfg: Config, profiles: list[Profile]) -> list[UsageSnapshot]:
    return list(
        await asyncio.gather(
            *(fetch_snapshot(cfg, p, previous=read_snapshot(p.id)) for p in profiles)
        )
    )


# ---------------------------------------------------------------- human output


def display_width(text: str) -> int:
    """Terminal columns: wide/fullwidth chars count 2, combining marks and VS16 count 0."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in ("️", "‍"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _money(value: float | None, currency: str | None) -> str:
    return format_money(value, currency)


def extra_line(extra: ExtraUsage) -> str:
    """`Extra usage 32% · €3.20 / €10.00`, or `Extra usage off[ · €0.00 / €10.00]`."""
    money = (
        f" · {_money(extra.used, extra.currency)} / {_money(extra.limit, extra.currency)}"
        if extra.limit is not None
        else ""
    )
    if extra.enabled:
        pct = f" {extra.percent}%" if extra.percent is not None else ""
        return f"Extra usage{pct}{money}"
    return f"Extra usage off{money}"


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "never"
    rel = format_relative(now, then)  # distance from `then` to `now`
    return "just now" if rel in ("now", "in <1m") else rel.removeprefix("in ") + " ago"


def status_line(snap: UsageSnapshot | None, profile: Profile, now: datetime) -> str | None:
    """One short line explaining a non-ok status (`None` when ok)."""
    if snap is None:
        return f"no data yet · {NO_DATA_HINT}"
    if snap.status == STATUS_OK:
        return None
    if snap.status == STATUS_STALE:
        return f"⚠ stale · updated {_ago(snap.newest_observed_at(), now)}"
    if snap.status == STATUS_NEEDS_SIGN_IN:
        return f"⚠ sign in required · run: ccs auth login --profile {profile.id}"
    if snap.status == STATUS_NO_SUBSCRIPTION:
        return "no plan limits (API key or no Claude subscription)"
    if snap.status == STATUS_SOURCE_ERROR:
        return f"⚠ usage unavailable: {snap.error or 'unknown error'}"
    return f"⚠ {snap.status}"


def render_human(
    items: list[tuple[Profile, UsageSnapshot | None]], now: datetime, tz: tzinfo
) -> str:
    """The `ccs usage` text block (manual 05)."""
    headers = [f"{p.emoji} {p.name}".strip() for p, _ in items]
    col = max((display_width(h) for h in headers), default=0) + 3
    blocks: list[str] = []
    for (profile, snap), header in zip(items, headers, strict=True):
        rows: list[tuple[str, int, datetime | None]] = []
        if snap is not None:
            if snap.session is not None:
                rows.append(("Session", snap.session.percent, snap.session.resets_at))
            if snap.weekly is not None:
                rows.append(("Weekly", snap.weekly.percent, snap.weekly.resets_at))
            rows += [(w.name, w.percent, w.resets_at) for w in snap.model_scoped]
        label_w = max((len(r[0]) for r in rows), default=0)
        lines: list[str] = []
        for label, pct, reset in rows:
            when = format_reset_combined(reset, now, tz) if reset is not None else NO_RESET
            lines.append(f"{label:<{label_w}} {pct:>3}%  {when}")
        if snap is not None and snap.extra_usage is not None:
            lines.append(extra_line(snap.extra_usage))
        note = status_line(snap, profile, now)
        if note:
            lines.append(note)
        pad = " " * col
        first = header + " " * (col - display_width(header))
        out = [first + lines[0]] + [pad + line for line in lines[1:]] if lines else [header]
        blocks.append("\n".join(line.rstrip() for line in out))
    return "\n".join(blocks)


# ---------------------------------------------------------------- command


def cmd_usage(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    try:
        cfg = store.ensure_config()
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    if args.profile:
        chosen = cfg.profile(args.profile)
        if chosen is None:
            return fail(as_json, f"unknown profile '{args.profile}'", code=EXIT_USAGE)
        profiles = [chosen]
    else:
        profiles = list(cfg.profiles)
    clock = SystemClock()
    source = SOURCE_FILE
    direct: dict[str, UsageSnapshot] = {}
    if args.refresh and profiles:
        if _refresh_via_daemon(profiles, args.profile):
            source = SOURCE_DAEMON
        else:
            source = SOURCE_DIRECT
            direct = {s.profile_id: s for s in asyncio.run(_direct(cfg, profiles))}
    now = to_utc_seconds(clock.now())
    items: list[tuple[Profile, UsageSnapshot | None]] = []
    for p in profiles:
        snap = direct[p.id] if p.id in direct else local_view(p.id, now)
        items.append((p, snap))
    if as_json:
        emit_json({"ok": True, "profiles": [_entry(p.id, s, source) for p, s in items]})
        return EXIT_OK
    if not items:
        print("no profiles configured · run: ccs profile add …")
        return EXIT_OK
    print(render_human(items, now, local_tz()))
    return EXIT_OK
