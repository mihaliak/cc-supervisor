"""The normalized `UsageSnapshot` (ADR-0005 `usage/<profile>.json`, schema 1).

Pure dataclasses. Timestamps are tz-aware UTC and serialize as `YYYY-MM-DDTHH:MM:SSZ`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SCHEMA = 1

STATUS_OK = "ok"
STATUS_NEEDS_SIGN_IN = "needs_sign_in"
STATUS_NO_SUBSCRIPTION = "no_subscription"
STATUS_SOURCE_ERROR = "source_error"
STATUS_STALE = "stale"
STATUSES = (
    STATUS_OK,
    STATUS_NEEDS_SIGN_IN,
    STATUS_NO_SUBSCRIPTION,
    STATUS_SOURCE_ERROR,
    STATUS_STALE,
)

SOURCE_GET_USAGE = "get_usage"
SOURCE_STATUSLINE = "statusline"


def to_utc_seconds(dt: datetime) -> datetime:
    """Aware UTC, truncated to whole seconds (absorbs `resets_at` sub-second jitter, P00-S2)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).replace(microsecond=0)


def format_iso(dt: datetime | None) -> str | None:
    """`2026-09-24T20:00:00Z`, or `None`."""
    if dt is None:
        return None
    return to_utc_seconds(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 string (any offset, fractional seconds) or epoch seconds → aware UTC seconds.

    Returns `None` for `None`, booleans, empty strings and anything unparseable.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        try:
            return datetime.fromtimestamp(float(value), UTC).replace(microsecond=0)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return to_utc_seconds(dt)
    return None


def round_percent(value: Any, *, clamp_max: int | None = 100) -> int | None:
    """Half-up integer percent, clamped to `0..clamp_max` (`None` = no upper clamp)."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    pct = math.floor(float(value) + 0.5)
    pct = max(pct, 0)
    if clamp_max is not None:
        pct = min(pct, clamp_max)
    return int(pct)


@dataclass(frozen=True)
class Window:
    """A session (five_hour) or weekly (seven_day) window."""

    percent: int
    resets_at: datetime | None
    observed_at: datetime
    source: str = SOURCE_GET_USAGE

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "resets_at": format_iso(self.resets_at),
            "observed_at": format_iso(self.observed_at),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: Any) -> Window | None:
        if not isinstance(d, dict):
            return None
        percent = round_percent(d.get("percent"))
        observed = parse_time(d.get("observed_at"))
        if percent is None or observed is None:
            return None
        source = d.get("source")
        return cls(
            percent,
            parse_time(d.get("resets_at")),
            observed,
            source if source in (SOURCE_GET_USAGE, SOURCE_STATUSLINE) else SOURCE_GET_USAGE,
        )


@dataclass(frozen=True)
class ScopedWindow:
    """A model-scoped weekly window (e.g. `Fable`)."""

    name: str
    percent: int
    resets_at: datetime | None
    observed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "percent": self.percent,
            "resets_at": format_iso(self.resets_at),
            "observed_at": format_iso(self.observed_at),
        }

    @classmethod
    def from_dict(cls, d: Any) -> ScopedWindow | None:
        if not isinstance(d, dict):
            return None
        name = d.get("name")
        percent = round_percent(d.get("percent"))
        observed = parse_time(d.get("observed_at"))
        if not isinstance(name, str) or not name or percent is None or observed is None:
            return None
        return cls(name, percent, parse_time(d.get("resets_at")), observed)


@dataclass(frozen=True)
class ExtraUsage:
    """Extra usage (paid credits), money in major units (converted via `decimal_places`)."""

    enabled: bool
    percent: int | None
    used: float | None
    limit: float | None
    currency: str | None
    disabled_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "percent": self.percent,
            "used": self.used,
            "limit": self.limit,
            "currency": self.currency,
            "disabled_reason": self.disabled_reason,
        }

    @classmethod
    def from_dict(cls, d: Any) -> ExtraUsage | None:
        if not isinstance(d, dict):
            return None

        def num(v: Any) -> float | None:
            if isinstance(v, bool) or not isinstance(v, int | float):
                return None
            return float(v)

        def text(v: Any) -> str | None:
            return v if isinstance(v, str) else None

        return cls(
            bool(d.get("enabled", False)),
            round_percent(d.get("percent"), clamp_max=None),
            num(d.get("used")),
            num(d.get("limit")),
            text(d.get("currency")),
            text(d.get("disabled_reason")),
        )


@dataclass(frozen=True)
class UsageSnapshot:
    """Normalized per-profile usage (ADR-0005)."""

    profile_id: str
    status: str
    error: str | None = None
    fetched_at: datetime | None = None
    polled_at: datetime | None = None
    subscription_type: str | None = None
    session: Window | None = None
    weekly: Window | None = None
    model_scoped: tuple[ScopedWindow, ...] = field(default_factory=tuple)
    extra_usage: ExtraUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "profile_id": self.profile_id,
            "status": self.status,
            "error": self.error,
            "fetched_at": format_iso(self.fetched_at),
            "polled_at": format_iso(self.polled_at),
            "subscription_type": self.subscription_type,
            "windows": {
                "session": self.session.to_dict() if self.session else None,
                "weekly": self.weekly.to_dict() if self.weekly else None,
                "model_scoped": [w.to_dict() for w in self.model_scoped],
            },
            "extra_usage": self.extra_usage.to_dict() if self.extra_usage else None,
        }

    @classmethod
    def from_dict(cls, d: Any) -> UsageSnapshot | None:
        """Tolerant parse; `None` when the object isn't a usable snapshot."""
        if not isinstance(d, dict):
            return None
        profile_id = d.get("profile_id")
        status = d.get("status")
        if not isinstance(profile_id, str) or not profile_id or status not in STATUSES:
            return None
        windows_raw = d.get("windows")
        windows: dict[str, Any] = windows_raw if isinstance(windows_raw, dict) else {}
        scoped_raw = windows.get("model_scoped")
        scoped: list[ScopedWindow] = []
        if isinstance(scoped_raw, list):
            for item in scoped_raw:
                w = ScopedWindow.from_dict(item)
                if w is not None:
                    scoped.append(w)
        error = d.get("error")
        sub = d.get("subscription_type")
        return cls(
            profile_id=profile_id,
            status=str(status),
            error=error if isinstance(error, str) else None,
            fetched_at=parse_time(d.get("fetched_at")),
            polled_at=parse_time(d.get("polled_at")),
            subscription_type=sub if isinstance(sub, str) else None,
            session=Window.from_dict(windows.get("session")),
            weekly=Window.from_dict(windows.get("weekly")),
            model_scoped=tuple(scoped),
            extra_usage=ExtraUsage.from_dict(d.get("extra_usage")),
        )

    def newest_observed_at(self) -> datetime | None:
        """Latest of `fetched_at` and every window's `observed_at`."""
        stamps = [self.fetched_at]
        stamps += [w.observed_at for w in (self.session, self.weekly) if w is not None]
        stamps += [w.observed_at for w in self.model_scoped]
        present = [s for s in stamps if s is not None]
        return max(present) if present else None
