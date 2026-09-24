from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ccs.clock import FakeClock, SystemClock, local_tz


def test_system_clock_is_utc_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_fake_clock_advance_and_set() -> None:
    start = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    mono = clock.monotonic()
    clock.advance(90)
    assert clock.now() == start + timedelta(seconds=90)
    assert clock.monotonic() == mono + 90
    clock.set(start + timedelta(hours=1))
    assert clock.now() == start + timedelta(hours=1)
    assert clock.monotonic() == mono + 3600


def test_fake_clock_requires_aware() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))


def test_local_tz_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Bratislava")
    time.tzset()
    try:
        tz = local_tz()
        assert isinstance(tz, ZoneInfo) and tz.key == "Europe/Bratislava"
        summer = datetime(2026, 7, 1, 12, tzinfo=UTC).astimezone(tz)
        winter = datetime(2026, 12, 1, 12, tzinfo=UTC).astimezone(tz)
        assert summer.utcoffset() == timedelta(hours=2)
        assert winter.utcoffset() == timedelta(hours=1)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_local_tz_bad_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Not/AZone")
    assert local_tz() is not None
