"""Statusline live reports and their merge into a `UsageSnapshot` (pure; ADR-0005).

P03 owns the live-report format (`schema/live-report.schema.json`); P07's statusline writes it:

    {schema: 1, profile_id, wrapper_id|null, session_id, model_id, effort|null, cwd,
     observed_at, rate_limits: {five_hour?: {percent, resets_at}, seven_day?: {…}}}

The parser is tolerant: `percent` or the raw statusline `used_percentage`; `resets_at` as
ISO 8601 or epoch seconds; unknown keys ignored.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ccs.usage.model import (
    SOURCE_STATUSLINE,
    STATUS_OK,
    STATUS_STALE,
    UsageSnapshot,
    Window,
    parse_time,
    round_percent,
    to_utc_seconds,
)
from ccs.usage.normalize import window_key_time

LIVE_REPORT_SCHEMA = 1
LIVE_MAX_AGE = timedelta(minutes=10)
STALE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class LiveWindow:
    """One `rate_limits` window as seen by a statusline."""

    percent: int
    resets_at: datetime | None


@dataclass(frozen=True)
class LiveReport:
    """A parsed `live/*.json` file."""

    profile_id: str | None
    wrapper_id: str | None
    session_id: str | None
    model_id: str | None
    effort: str | None
    cwd: str | None
    observed_at: datetime
    five_hour: LiveWindow | None
    seven_day: LiveWindow | None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _live_window(raw: Any) -> LiveWindow | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("percent", raw.get("used_percentage"))
    percent = round_percent(value)
    if percent is None:
        return None
    return LiveWindow(percent, parse_time(raw.get("resets_at")))


def parse_live_report(d: Any) -> LiveReport | None:
    """Parse one live report dict; `None` if it has no usable `observed_at`."""
    if not isinstance(d, dict):
        return None
    observed = parse_time(d.get("observed_at"))
    if observed is None:
        return None
    rl = d.get("rate_limits")
    rl = rl if isinstance(rl, dict) else {}
    effort = d.get("effort")
    if isinstance(effort, dict):  # tolerate the raw statusline `{"level": …}` shape
        effort = effort.get("level")
    return LiveReport(
        profile_id=_str(d.get("profile_id")),
        wrapper_id=_str(d.get("wrapper_id")),
        session_id=_str(d.get("session_id")),
        model_id=_str(d.get("model_id")),
        effort=_str(effort),
        cwd=_str(d.get("cwd")),
        observed_at=observed,
        five_hour=_live_window(rl.get("five_hour")),
        seven_day=_live_window(rl.get("seven_day")),
    )


def same_instance(a: datetime | None, b: datetime | None) -> bool:
    """Whether two `resets_at` values name the same window instance (minute-rounded)."""
    return a is not None and b is not None and window_key_time(a) == window_key_time(b)


def _candidate(
    current: Window | None, live: LiveWindow | None, observed_at: datetime, now: datetime
) -> Window | None:
    """The current window or a live one, whichever describes usage best (ADR-0022).

    - Same window instance: the higher percent wins (usage only rises within a window, so a
      stale statusline redraw can't lower fresher poll data); equal percents keep the newer.
    - Different windows: strictly newer `observed_at` wins; ties keep current.
    """
    if live is None:
        return current
    if live.resets_at is not None and live.resets_at <= now:
        return current  # that window already ended; its percent is meaningless now
    if current is not None:
        if same_instance(current.resets_at, live.resets_at) and live.percent != current.percent:
            keep = live.percent < current.percent
        else:
            keep = observed_at <= current.observed_at
        if keep:
            return current
    return Window(live.percent, live.resets_at, observed_at, SOURCE_STATUSLINE)


def merge(
    snapshot: UsageSnapshot | None,
    reports: Sequence[LiveReport],
    *,
    now: datetime,
    profile_id: str | None = None,
) -> UsageSnapshot | None:
    """Overlay fresh live reports onto the polled snapshot.

    - `session` / `weekly` only: within one window instance the higher percent wins,
      otherwise the candidate with the newest `observed_at` (`_candidate`).
    - Reports older than 10 min, for another profile, or for an ended window are ignored.
    - `model_scoped` and `extra_usage` always come from the snapshot.
    - Without a snapshot, fresh reports alone produce an `ok` snapshot (no `fetched_at`);
      `None` when there is nothing at all.
    """
    pid = snapshot.profile_id if snapshot is not None else profile_id
    fresh = [
        r
        for r in reports
        if now - r.observed_at <= LIVE_MAX_AGE
        and (r.profile_id is None or pid is None or r.profile_id == pid)
    ]
    if snapshot is None:
        if not fresh or pid is None:
            return None
        base = UsageSnapshot(profile_id=pid, status=STATUS_OK)
    else:
        base = snapshot
    session, weekly = base.session, base.weekly
    for report in sorted(fresh, key=lambda r: r.observed_at):
        session = _candidate(session, report.five_hour, report.observed_at, now)
        weekly = _candidate(weekly, report.seven_day, report.observed_at, now)
    if session is base.session and weekly is base.weekly:
        return base
    return dataclasses.replace(base, session=session, weekly=weekly)


def apply_staleness(snapshot: UsageSnapshot, now: datetime) -> UsageSnapshot:
    """`ok` → `stale` when neither a poll nor a window observation is newer than 10 min."""
    if snapshot.status != STATUS_OK:
        return snapshot
    newest = snapshot.newest_observed_at()
    if newest is None or newest < to_utc_seconds(now) - STALE_AFTER:
        return dataclasses.replace(snapshot, status=STATUS_STALE)
    return snapshot
