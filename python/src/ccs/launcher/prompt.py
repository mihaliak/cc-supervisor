"""Start-while-held check (ADR-0007): ask before starting claude while the profile is paused."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from ccs import timefmt


@dataclass(frozen=True)
class HeldInfo:
    """The profile's active holds (hold ids) and the latest known resume time."""

    holds: tuple[str, ...]
    resume_at: datetime | None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _hold_parts(hold: Any) -> tuple[str | None, str | None, datetime | None]:
    """`(id, scope, resets_at)` of a hold given as a string id or a dict."""
    if isinstance(hold, str):
        return hold, None, None
    if not isinstance(hold, dict):
        return None, None, None
    hid = hold.get("id") or hold.get("kind")
    scope = hold.get("scope")
    resets = _parse_iso(hold.get("resets_at")) or _parse_iso(hold.get("resume_at"))
    return (
        hid if isinstance(hid, str) else None,
        scope if isinstance(scope, str) else None,
        resets,
    )


def held_info(status_reply: dict[str, Any] | None, profile_id: str) -> HeldInfo | None:
    """Active profile-level holds from a daemon `status` reply, or `None` when not held.

    Holds may be ids (`"session"`) or objects (`{"id", "scope", "resets_at"}`). A `manual`
    hold scoped to one wrapper does not hold the whole profile.
    """
    if not isinstance(status_reply, dict):
        return None
    profiles = status_reply.get("profiles")
    entry = next(
        (p for p in profiles or [] if isinstance(p, dict) and p.get("id") == profile_id),
        None,
    )
    if entry is None:
        return None
    sup = entry.get("supervisor")
    if not isinstance(sup, dict):
        return None
    ids: list[str] = []
    resets: list[datetime] = []
    for hold in sup.get("holds") or []:
        hid, scope, resets_at = _hold_parts(hold)
        if hid is None:
            continue
        if hid == "manual" and scope is not None:
            continue  # session-scoped manual hold (scope = wrapper id)
        ids.append(hid)
        if resets_at is not None:
            resets.append(resets_at)
    if not ids:
        return None
    resume_at = _parse_iso(sup.get("resume_at"))
    if resets:
        latest = max(resets)
        resume_at = latest if resume_at is None or latest > resume_at else resume_at
    return HeldInfo(holds=tuple(ids), resume_at=resume_at)


def held_prompt(profile_name: str, info: HeldInfo, now: datetime, tz: tzinfo) -> str:
    """The `[y/N]` question shown before starting a held profile."""
    if info.resume_at is None:
        return f"Profile {profile_name} is paused (manual). Start anyway? [y/N] "
    until = timefmt.format_reset_combined(info.resume_at, now, tz)
    return f"Profile {profile_name} is paused until {until}. Start anyway? [y/N] "


def is_yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


def ask_yes_no(question: str, in_fd: int, out_fd: int) -> bool:
    """Write `question`, read one line (cooked mode) from `in_fd`; True for `y`/`yes`."""
    os.write(out_fd, question.encode("utf-8"))
    line = bytearray()
    while True:
        try:
            chunk = os.read(in_fd, 1)
        except InterruptedError:
            continue
        except OSError:
            break
        if not chunk:
            break
        line += chunk
        if chunk in (b"\n", b"\r"):
            break
    return is_yes(line.decode("utf-8", errors="replace"))
