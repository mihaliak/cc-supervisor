"""`widget/snapshot.json`: the display model for widgets and the menu bar (ADR-0005/0009).

Pure builder (`build_widget_snapshot`) plus a tiny writer. Python precomputes levels and
money strings (ADR-0001); Swift formats only the timestamps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ccs import fsio, paths
from ccs.config.models import Colors, Config, Profile
from ccs.usage.model import STATUS_STALE, UsageSnapshot, format_iso

SCHEMA = 1
NO_DATA = "no_data"
LEVELS = ("green", "yellow", "red")

CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥"}

SUPERVISOR_KEYS = (
    "state",
    "active_sessions",
    "paused_sessions",
    "other_sessions",
    "resume_at",
    "next_warmup_at",
)


def level_for(percent: int, colors: Colors) -> str:
    """`green` below `yellow_from`, `yellow` below `red_from`, else `red` (ADR-0009)."""
    return colors.level(percent)


def worst_level(levels: Sequence[str]) -> str | None:
    """The most severe level, or `None` for no levels."""
    if not levels:
        return None
    return max(levels, key=LEVELS.index)


def format_money(value: float | None, currency: str | None) -> str:
    """`€3.20`, `$3.20`, `3.20 CHF` (unknown code), `3.20` (no currency)."""
    amount = f"{value or 0:.2f}"
    if currency in CURRENCY_SYMBOLS:
        return f"{CURRENCY_SYMBOLS[currency]}{amount}"
    return f"{amount} {currency}" if currency else amount


def _iso(value: Any) -> Any:
    return format_iso(value) if isinstance(value, datetime) else value


def _row(
    kind: str, label: str, percent: int, resets_at: datetime | None, colors: Colors
) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "percent": percent,
        "level": level_for(percent, colors),
        "resets_at": format_iso(resets_at),
    }


def build_rows(
    profile: Profile, snap: UsageSnapshot | None, colors: Colors
) -> list[dict[str, Any]]:
    """Session, weekly, each model-scoped window, then extra usage (enabled or spill set)."""
    if snap is None:
        return []
    rows: list[dict[str, Any]] = []
    if snap.session is not None:
        rows.append(
            _row("session", "Session", snap.session.percent, snap.session.resets_at, colors)
        )
    if snap.weekly is not None:
        rows.append(_row("weekly", "Weekly", snap.weekly.percent, snap.weekly.resets_at, colors))
    for w in snap.model_scoped:
        rows.append(_row("model_scoped", w.name, w.percent, w.resets_at, colors))
    extra = snap.extra_usage
    if extra is not None and (extra.enabled or profile.limits.extra_usage.spill):
        row = _row("extra_usage", "Extra usage", extra.percent or 0, None, colors)
        if extra.limit is not None:
            used = format_money(extra.used, extra.currency)
            row["detail"] = f"{used} / {format_money(extra.limit, extra.currency)}"
        rows.append(row)
    return rows


def default_supervisor(
    sessions: Sequence[Mapping[str, Any]], other_sessions: Mapping[str, int] | None
) -> dict[str, Any]:
    """Supervisor fields before P06/P08 contribute: counts from the session registry."""
    paused = 0
    for rec in sessions:
        sup = rec.get("supervision")
        if isinstance(sup, Mapping) and sup.get("state") == "paused":
            paused += 1
    others = sum(v for v in (other_sessions or {}).values() if isinstance(v, int))
    return {
        "state": "paused" if paused else "normal",
        "active_sessions": len(sessions),
        "paused_sessions": paused,
        "other_sessions": others,
        "resume_at": None,
        "next_warmup_at": None,
    }


def build_profile(
    profile: Profile,
    snap: UsageSnapshot | None,
    colors: Colors,
    *,
    sessions: Sequence[Mapping[str, Any]] = (),
    other_sessions: Mapping[str, int] | None = None,
    supervisor: Mapping[str, Any] | None = None,
    next_warmup_at: datetime | None = None,
) -> dict[str, Any]:
    """One `profiles[]` entry of the widget snapshot."""
    rows = build_rows(profile, snap, colors)
    sup = default_supervisor(sessions, other_sessions)
    sup["next_warmup_at"] = format_iso(next_warmup_at)
    for key, value in (supervisor or {}).items():
        if key in SUPERVISOR_KEYS:
            sup[key] = _iso(value)
    updated = snap.newest_observed_at() if snap is not None else None
    return {
        "id": profile.id,
        "name": profile.name,
        "emoji": profile.emoji,
        "status": snap.status if snap is not None else NO_DATA,
        "stale": bool(snap is not None and snap.status == STATUS_STALE),
        "level": worst_level([r["level"] for r in rows]),
        "updated_at": format_iso(updated),
        "rows": rows,
        "supervisor": sup,
    }


def build_widget_snapshot(
    config: Config,
    snapshots: Mapping[str, UsageSnapshot | None],
    supervisor_states: Mapping[str, Mapping[str, Any]],
    sessions: Mapping[str, Sequence[Mapping[str, Any]]],
    other_sessions: Mapping[str, Mapping[str, int]],
    next_warmups: Mapping[str, datetime | None],
    now: datetime,
) -> dict[str, Any]:
    """The whole `widget/snapshot.json` document (profiles in config order)."""
    colors = config.display.colors
    return {
        "schema": SCHEMA,
        "generated_at": format_iso(now),
        "profiles": [
            build_profile(
                p,
                snapshots.get(p.id),
                colors,
                sessions=sessions.get(p.id, ()),
                other_sessions=other_sessions.get(p.id),
                supervisor=supervisor_states.get(p.id),
                next_warmup_at=next_warmups.get(p.id),
            )
            for p in config.profiles
        ],
    }


def write_widget_snapshot(doc: Mapping[str, Any]) -> None:
    """Atomically write `widget/snapshot.json` (0644 so the sandboxed widget can read it)."""
    fsio.atomic_write_json(paths.widget_snapshot(), dict(doc), mode=0o644)


def read_widget_snapshot() -> dict[str, Any] | None:
    """Tolerant read of `widget/snapshot.json`."""
    return fsio.read_json(paths.widget_snapshot())
