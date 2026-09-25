"""Reset-time formatting per ADR-0009 (pure, locale-independent).

Shared test vectors: `schema/fixtures/time_format.json` (Swift uses the same file).
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_reset_absolute(reset: datetime, now: datetime, tz: tzinfo) -> str:
    """`HH:MM` today, `Ddd HH:MM` within the next 6 local days, else `D Mon HH:MM`.

    The clock time is rounded to the nearest minute: Claude reports resets a moment off the
    minute (`07:59:59.9` for 08:00), which must not show as `07:59`.
    """
    try:
        r = (reset + timedelta(seconds=30)).astimezone(tz)
    except OverflowError:  # at the edge of the datetime range: shown as given, unrounded
        r = reset
    n = now.astimezone(tz)
    hm = f"{r.hour:02d}:{r.minute:02d}"
    days = (r.date() - n.date()).days
    if days == 0:
        return hm
    if 0 < days < 7:
        return f"{WEEKDAYS[r.weekday()]} {hm}"
    return f"{r.day} {MONTHS[r.month - 1]} {hm}"


def format_relative(reset: datetime, now: datetime) -> str:
    """`now`, `in <1m`, `in 42m`, `in 2h 13m`, `in 2h`, `in 3d 4h`, `in 3d` (rounded down)."""
    seconds = (reset - now).total_seconds()
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return "in <1m"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"in {minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h" if mins == 0 else f"in {hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"in {days}d" if hours == 0 else f"in {days}d {hours}h"


def format_reset_combined(reset: datetime, now: datetime, tz: tzinfo) -> str:
    """Statusline form: `20:00 (in 2h 13m)`."""
    return f"{format_reset_absolute(reset, now, tz)} ({format_relative(reset, now)})"


def format_reset_compact(reset: datetime, now: datetime, tz: tzinfo) -> str:
    """Widget / menu bar form: `20:00 · in 2h 13m`."""
    return f"{format_reset_absolute(reset, now, tz)} · {format_relative(reset, now)}"
