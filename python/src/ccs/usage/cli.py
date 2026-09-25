"""`ccs usage [--profile <id>] [--refresh] [--json]` (ADR-0017)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
import unicodedata
from collections.abc import Collection
from datetime import datetime, tzinfo
from typing import Any

from ccs import paths
from ccs.clock import Clock, SystemClock, local_tz
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
SOURCE_DAEMON_PENDING = "daemon_pending"  # asked the daemon, its poll didn't land in time
SOURCE_DIRECT = "direct_probe"
REFRESH_WAIT_S = 20.0
NO_DATA_HINT = "run: ccs usage --refresh"
NO_RESET = "\u2013"  # en dash: window without a reset time
WINDOW_RESET = "reset"  # the window's reset time has passed
PENDING_NOTE = "⚠ refresh not finished after {wait} s · showing the last saved data"


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


# (file identity: dev, inode, mtime_ns, size; `polled_at`) of `usage/<id>.json`
_PollMark = tuple[tuple[int, int, int, int] | None, datetime | None]


def _refresh_via_daemon(
    profiles: list[Profile], target: str | None, clock: Clock
) -> set[str] | None:
    """Ask a running daemon to poll, then wait (≤ 20 s) until each profile's poll has landed.

    `None` when no daemon took the request (probe directly instead). Otherwise the ids whose
    refresh didn't land in time: their usage file still holds older data.
    """
    before = {p.id: _poll_mark(p.id) for p in profiles}
    requested = to_utc_seconds(clock.now())
    try:
        with DaemonClient(timeout=2.0) as client:
            if not client.hello().get("ok"):
                return None
            fields: dict[str, Any] = {"profile_id": target} if target else {}
            reply = client.request("refresh", **fields)
    except DaemonUnavailable:
        return None
    if not reply.get("ok"):
        return None
    deadline = time.monotonic() + REFRESH_WAIT_S
    pending = set(before)
    while True:
        pending = {pid for pid in pending if not _landed(before[pid], _poll_mark(pid), requested)}
        if not pending or time.monotonic() >= deadline:
            return pending
        time.sleep(0.25)


def _poll_mark(profile_id: str) -> _PollMark:
    key: tuple[int, int, int, int] | None = None
    with contextlib.suppress(OSError):
        st = os.stat(paths.usage_file(profile_id))
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    snap = read_snapshot(profile_id)
    return key, snap.polled_at if snap is not None else None


def _landed(before: _PollMark, now: _PollMark, requested: datetime) -> bool:
    """A poll finished since the refresh request.

    `polled_at` has whole seconds (the schema's format), so a poll ending in the same second
    as the previous one only shows as a rewritten file whose `polled_at` isn't older than the
    request's second. (A live-report rewrite in that second counts too; its poll data is
    then less than a second older than the request.)
    """
    key, polled = now
    old_key, old_polled = before
    if polled is None:
        return False
    if old_polled is None or polled > old_polled:
        return True
    return key != old_key and polled >= requested


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


def window_cell(pct: int, reset: datetime | None, now: datetime, tz: tzinfo) -> str:
    """`45%  20:00 (in 2h 13m)`; a window whose reset has passed shows as `?%  reset`.

    Like the statusline: an ended window's numbers no longer apply, and the new window's
    arrive with the next poll.
    """
    if reset is None:
        return f"{pct:>3}%  {NO_RESET}"
    if reset <= now:
        return f"{'?':>3}%  {WINDOW_RESET}"
    return f"{pct:>3}%  {format_reset_combined(reset, now, tz)}"


def render_human(
    items: list[tuple[Profile, UsageSnapshot | None]],
    now: datetime,
    tz: tzinfo,
    *,
    pending: Collection[str] = (),
) -> str:
    """The `ccs usage` text block (manual 05). `pending`: ids whose refresh didn't land."""
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
            lines.append(f"{label:<{label_w}} {window_cell(pct, reset, now, tz)}")
        if snap is not None and snap.extra_usage is not None:
            lines.append(extra_line(snap.extra_usage))
        note = status_line(snap, profile, now)
        if note:
            lines.append(note)
        if profile.id in pending:
            lines.append(PENDING_NOTE.format(wait=f"{REFRESH_WAIT_S:g}"))
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
    pending: set[str] = set()
    if args.refresh and profiles:
        left = _refresh_via_daemon(profiles, args.profile, clock)
        if left is not None:
            source = SOURCE_DAEMON
            pending = left
        else:
            source = SOURCE_DIRECT
            direct = {s.profile_id: s for s in asyncio.run(_direct(cfg, profiles))}
    now = to_utc_seconds(clock.now())
    items: list[tuple[Profile, UsageSnapshot | None]] = []
    for p in profiles:
        snap = direct[p.id] if p.id in direct else local_view(p.id, now)
        items.append((p, snap))
    if as_json:
        entries = [
            _entry(p.id, s, SOURCE_DAEMON_PENDING if p.id in pending else source) for p, s in items
        ]
        emit_json({"ok": True, "profiles": entries})
        return EXIT_OK
    if not items:
        print("no profiles configured · run: ccs profile add …")
        return EXIT_OK
    print(render_human(items, now, local_tz(), pending=pending))
    return EXIT_OK
