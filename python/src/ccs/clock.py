"""Injectable clock (ADR-0013: the core never calls `datetime.now()` directly)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Clock(Protocol):
    """Source of wall-clock (tz-aware UTC) and monotonic time."""

    def now(self) -> datetime:
        """Current time, tz-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds for measuring durations."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Current time, tz-aware UTC."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """`time.monotonic()`."""
        return time.monotonic()


class FakeClock:
    """A manually advanced clock for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be tz-aware")
        self._now = start.astimezone(UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        """The fake current time."""
        return self._now

    def monotonic(self) -> float:
        """Fake monotonic seconds; moves with `advance`."""
        return self._mono

    def advance(self, seconds: float) -> None:
        """Move both clocks forward."""
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, when: datetime) -> None:
        """Jump the wall clock (monotonic moves by the same delta if forward, else stays)."""
        when = when.astimezone(UTC)
        delta = (when - self._now).total_seconds()
        if delta > 0:
            self._mono += delta
        self._now = when


def _zone_from_localtime_link() -> ZoneInfo | None:
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    idx = target.find(marker)
    if idx < 0:
        return None
    try:
        return ZoneInfo(target[idx + len(marker) :])
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_tz() -> tzinfo:
    """The DST-aware local zone: `$TZ` if it names an IANA zone, else `/etc/localtime`.

    Falls back to the current fixed offset only when no zone name can be found.
    """
    name = os.environ.get("TZ", "")
    if name:
        name = name.removeprefix(":")
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    zone = _zone_from_localtime_link()
    if zone is not None:
        return zone
    return datetime.now().astimezone().tzinfo or UTC
