"""Raw `get_usage` payload → `UsageSnapshot` (pure; ADR-0002, P00-S2 shapes).

All knowledge of Claude Code's experimental payload lives here, with fixture tests.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

from ccs.usage.model import (
    SOURCE_GET_USAGE,
    STATUS_OK,
    ExtraUsage,
    ScopedWindow,
    UsageSnapshot,
    Window,
    parse_time,
    round_percent,
    to_utc_seconds,
)


class MalformedPayload(ValueError):
    """The payload lacks or mistypes a field the normalizer needs."""


WINDOW_KEY_FORMAT = "%Y-%m-%dT%H:%MZ"


def window_key_time(dt: datetime) -> str:
    """`dt` rounded to the nearest minute as `YYYY-MM-DDTHH:MMZ`.

    P06 builds every window-instance key from this. Rounding absorbs jitter only away from
    the `:30` edge (`12:00:29.8` → `12:00`, `12:00:30.2` → `12:01`), so the policy matches a
    new `resets_at` against the keys it already knows within a tolerance
    (`policy.match_instance`) before it treats the window as new.
    """
    rounded = to_utc_seconds(dt + timedelta(seconds=30)).replace(second=0)
    return rounded.strftime(WINDOW_KEY_FORMAT)


def parse_window_key_time(text: str) -> datetime | None:
    """The UTC minute a `window_key_time` string names (None if it isn't one)."""
    try:
        return datetime.strptime(text, WINDOW_KEY_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _window(raw: Any, key: str, observed_at: datetime) -> Window | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MalformedPayload(f"rate_limits.{key} is not an object")
    utilization = raw.get("utilization")
    if utilization is None:
        return None
    percent = round_percent(utilization)
    if percent is None:
        raise MalformedPayload(f"rate_limits.{key}.utilization is not a number")
    resets_raw = raw.get("resets_at")
    resets_at = parse_time(resets_raw)
    if resets_raw is not None and resets_at is None:
        raise MalformedPayload(f"rate_limits.{key}.resets_at is not a timestamp")
    return Window(percent, resets_at, observed_at, SOURCE_GET_USAGE)


def _scoped_primary(items: list[Any], observed_at: datetime) -> list[ScopedWindow]:
    out: list[ScopedWindow] = []
    for item in items:
        if not isinstance(item, dict):
            raise MalformedPayload("rate_limits.model_scoped[] entry is not an object")
        name = item.get("display_name")
        percent = round_percent(item.get("utilization"))
        if not isinstance(name, str) or not name or percent is None:
            continue
        out.append(ScopedWindow(name, percent, parse_time(item.get("resets_at")), observed_at))
    return out


def _scoped_from_limits(limits: Any, observed_at: datetime) -> list[ScopedWindow]:
    if not isinstance(limits, list):
        return []
    out: list[ScopedWindow] = []
    for item in limits:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        percent = round_percent(item.get("percent"))
        if not isinstance(name, str) or not name or percent is None:
            continue
        out.append(ScopedWindow(name, percent, parse_time(item.get("resets_at")), observed_at))
    return out


def _dedupe(windows: list[ScopedWindow]) -> tuple[ScopedWindow, ...]:
    seen: set[str] = set()
    out: list[ScopedWindow] = []
    for w in windows:
        if w.name in seen:
            continue
        seen.add(w.name)
        out.append(w)
    return tuple(out)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _extra_usage(raw: Any) -> ExtraUsage | None:
    if not isinstance(raw, dict):
        return None
    dp_raw = raw.get("decimal_places")
    dp = dp_raw if isinstance(dp_raw, int) and not isinstance(dp_raw, bool) and dp_raw >= 0 else 2
    scale = 10**dp
    used_minor = _num(raw.get("used_credits"))
    limit_minor = _num(raw.get("monthly_limit"))
    used = round(used_minor / scale, dp) if used_minor is not None else None
    limit = round(limit_minor / scale, dp) if limit_minor is not None else None
    percent = round_percent(raw.get("utilization"), clamp_max=None)
    if percent is None and used is not None and limit:
        percent = round_percent(used / limit * 100, clamp_max=None)
    currency = raw.get("currency")
    reason = raw.get("disabled_reason")
    return ExtraUsage(
        enabled=bool(raw.get("is_enabled")),
        percent=percent,
        used=used,
        limit=limit,
        currency=currency if isinstance(currency, str) else None,
        disabled_reason=reason if isinstance(reason, str) else None,
    )


def normalize(raw: dict[str, Any], *, profile_id: str, fetched_at: datetime) -> UsageSnapshot:
    """Map a successful `get_usage` response (`response.response`) to an `ok` snapshot.

    Raises `MalformedPayload` when `rate_limits` is missing or mistyped. Callers decide
    `needs_sign_in` / `no_subscription` before calling (see `source_claude.classify`).
    """
    if not isinstance(raw, dict):
        raise MalformedPayload("payload is not an object")
    rl = raw.get("rate_limits")
    if not isinstance(rl, dict):
        raise MalformedPayload("rate_limits is missing")
    observed = to_utc_seconds(fetched_at)
    session = _window(rl.get("five_hour"), "five_hour", observed)
    weekly = _window(rl.get("seven_day"), "seven_day", observed)
    primary = rl.get("model_scoped")
    if primary is not None and not isinstance(primary, list):
        raise MalformedPayload("rate_limits.model_scoped is not a list")
    if isinstance(primary, list):
        scoped = _scoped_primary(primary, observed)
    else:
        scoped = _scoped_from_limits(rl.get("limits"), observed)
    sub = raw.get("subscription_type")
    return UsageSnapshot(
        profile_id=profile_id,
        status=STATUS_OK,
        error=None,
        fetched_at=observed,
        polled_at=observed,
        subscription_type=sub if isinstance(sub, str) else None,
        session=session,
        weekly=weekly,
        model_scoped=_dedupe(scoped),
        extra_usage=_extra_usage(rl.get("extra_usage")),
    )


def session_window_active(snapshot: UsageSnapshot | None, now: datetime) -> bool:
    """True only while a session window exists and its `resets_at` is in the future.

    A missing window, a `null` `resets_at`, or a past `resets_at` all mean inactive (P00-S2).
    """
    if snapshot is None or snapshot.session is None or snapshot.session.resets_at is None:
        return False
    return snapshot.session.resets_at > now


def snapshot_from_error(
    profile_id: str,
    status: str,
    error: str | None,
    previous: UsageSnapshot | None,
    now: datetime,
) -> UsageSnapshot:
    """A failed poll: keep the previous windows (no blanking), set status/error/`polled_at`.

    `fetched_at` stays the last success.
    """
    polled = to_utc_seconds(now)
    if previous is not None and previous.profile_id == profile_id:
        return dataclasses.replace(previous, status=status, error=error, polled_at=polled)
    return UsageSnapshot(profile_id=profile_id, status=status, error=error, polled_at=polled)
