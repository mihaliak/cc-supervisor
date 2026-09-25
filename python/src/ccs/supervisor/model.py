"""Supervisor data: holds, the persisted per-profile state, and session views (ADR-0005/0008).

`supervisor/<profile>.json` holds `ProfileSupervisorState` (schema 1). Session records
(`sessions/<wrapper_id>.json`) are read through `SessionView`. Everything here is plain data
with tolerant (de)serialization; the policy (`policy.py`) never does IO.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ccs.usage.model import format_iso, parse_time

SCHEMA = 1
BOUND_KEYS = 500
BOUND_LEDGER = 200

# hold kinds (ADR-0005 hold ids are `kind` or `model_scoped:<Name>`)
SESSION = "session"
WEEKLY = "weekly"
MODEL_SCOPED = "model_scoped"
EXTRA_USAGE = "extra_usage"
MANUAL = "manual"
KINDS = (SESSION, WEEKLY, MODEL_SCOPED, EXTRA_USAGE, MANUAL)

# session supervision states
RUNNING = "running"
PAUSED = "paused"
OVERRIDDEN = "overridden"
STATES = (RUNNING, PAUSED, OVERRIDDEN)

LEDGER_ACTIONS = (
    "pause",
    "resume",
    "override",
    "time_based_clear",
    "manual_pause",
    "manual_resume",
    "release",
)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def extra_instance(limit: float | None, arm: int) -> str:
    """`extra_usage:<cap>:<arm>`: a new cap or a re-arm (drop below warn) is a new instance."""
    cap = "none" if limit is None else f"{limit:g}"
    return f"{EXTRA_USAGE}:{cap}:{arm}"


# Before ADR-0022 the credits instance was `extra_usage:<YYYY-MM>:<cap>` (a calendar month
# re-armed it). Stored keys are read as arm 0 of their cap, which is where a fresh state starts,
# so an upgrade never re-fires a warn or pause that already happened.
_LEGACY_EXTRA = re.compile(rf"((?:warnp?:)?){EXTRA_USAGE}:[0-9]{{4}}-[0-9]{{2}}:([^:]+)")


def upgrade_key(key: str) -> str:
    """A pre-ADR-0022 `extra_usage` instance (or its warn key) in the current format."""
    m = _LEGACY_EXTRA.fullmatch(key)
    return f"{m[1]}{EXTRA_USAGE}:{m[2]}:0" if m else key


def bounded(items: Iterable[str], limit: int = BOUND_KEYS) -> tuple[str, ...]:
    """Unique items in first-seen order, keeping only the newest `limit`."""
    seen: dict[str, None] = {}
    for item in items:
        seen.pop(item, None)
        seen[item] = None
    keys = list(seen)
    return tuple(keys[-limit:])


def _keys(value: Any) -> tuple[str, ...]:
    return bounded(upgrade_key(k) for k in _str_list(value))


@dataclass(frozen=True)
class Hold:
    """One active reason to keep sessions paused."""

    id: str  # `session` | `weekly` | `model_scoped:<Name>` | `extra_usage` | `manual`
    kind: str
    instance: str  # window-instance key (dedupe / hysteresis)
    scope: str | None = None  # model name (model_scoped) or wrapper id (session manual hold)
    resets_at: datetime | None = None
    created_at: datetime | None = None
    confirm_started_at: datetime | None = None
    next_confirm_poll_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "instance": self.instance,
            "scope": self.scope,
            "resets_at": format_iso(self.resets_at),
            "created_at": format_iso(self.created_at),
            "confirm_started_at": format_iso(self.confirm_started_at),
            "next_confirm_poll_at": format_iso(self.next_confirm_poll_at),
        }

    def public(self) -> dict[str, Any]:
        """The shape published in `status` / snapshot contributions (ids + scope + reset)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "instance": self.instance,
            "scope": self.scope,
            "resets_at": format_iso(self.resets_at),
        }

    @classmethod
    def from_dict(cls, d: Any) -> Hold | None:
        if not isinstance(d, Mapping):
            return None
        hid, kind, instance = _str(d.get("id")), _str(d.get("kind")), _str(d.get("instance"))
        if hid is None or instance is None:
            return None
        instance = upgrade_key(instance)
        if kind is None:
            kind = hid.split(":", 1)[0]
        if kind not in KINDS:
            return None
        return cls(
            id=hid,
            kind=kind,
            instance=instance,
            scope=_str(d.get("scope")),
            resets_at=parse_time(d.get("resets_at")),
            created_at=parse_time(d.get("created_at")),
            confirm_started_at=parse_time(d.get("confirm_started_at")),
            next_confirm_poll_at=parse_time(d.get("next_confirm_poll_at")),
        )


@dataclass(frozen=True)
class LedgerEntry:
    """One supervisor action, for `supervisor/<profile>.json` history."""

    ts: datetime
    action: str
    instance: str | None = None
    wrapper_ids: tuple[str, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": format_iso(self.ts),
            "action": self.action,
            "instance": self.instance,
            "wrapper_ids": list(self.wrapper_ids),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: Any) -> LedgerEntry | None:
        if not isinstance(d, Mapping):
            return None
        ts = parse_time(d.get("ts"))
        action = _str(d.get("action"))
        if ts is None or action is None:
            return None
        return cls(
            ts=ts,
            action=action,
            instance=_str(d.get("instance")),
            wrapper_ids=tuple(_str_list(d.get("wrapper_ids"))),
            detail=_str(d.get("detail")),
        )


@dataclass(frozen=True)
class ProfileSupervisorState:
    """`supervisor/<profile>.json`: active holds plus dedupe / hysteresis memory.

    `extra_usage_arm` numbers the credits window instance (`extra_instance`); the policy
    bumps it when `percent` drops below warn after the instance fired (ADR-0022).
    """

    holds: tuple[Hold, ...] = ()
    warned: tuple[str, ...] = ()
    paused_instances: tuple[str, ...] = ()
    released_instances: tuple[str, ...] = ()
    ledger: tuple[LedgerEntry, ...] = ()
    extra_usage_arm: int = 0

    def to_dict(self, profile_id: str) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "profile_id": profile_id,
            "holds": [h.to_dict() for h in self.holds],
            "warned": list(self.warned),
            "paused_instances": list(self.paused_instances),
            "released_instances": list(self.released_instances),
            "ledger": [e.to_dict() for e in self.ledger],
            "extra_usage_arm": self.extra_usage_arm,
        }

    @classmethod
    def from_dict(cls, d: Any) -> ProfileSupervisorState:
        """Tolerant parse; an unusable document is an empty state."""
        if not isinstance(d, Mapping):
            return cls()
        holds = [Hold.from_dict(h) for h in d.get("holds") or []]
        ledger = [LedgerEntry.from_dict(e) for e in d.get("ledger") or []]
        arm = d.get("extra_usage_arm")
        if not isinstance(arm, int) or isinstance(arm, bool) or arm < 0:
            arm = 0
        return cls(
            holds=tuple(h for h in holds if h is not None),
            warned=_keys(d.get("warned")),
            paused_instances=_keys(d.get("paused_instances")),
            released_instances=_keys(d.get("released_instances")),
            ledger=tuple(e for e in ledger if e is not None)[-BOUND_LEDGER:],
            extra_usage_arm=arm,
        )

    def with_ledger(self, entries: Sequence[LedgerEntry]) -> tuple[LedgerEntry, ...]:
        return (*self.ledger, *entries)[-BOUND_LEDGER:]


@dataclass(frozen=True)
class Supervision:
    """The `supervision` block of a session record.

    `pending_override_instances`: model-scoped hold instances a session was started anyway
    against while its model was still unknown; they count as overridden once the model is
    known and matches (ADR-0022).
    """

    state: str = RUNNING
    holds: tuple[str, ...] = ()
    paused_at: datetime | None = None
    resume_at: datetime | None = None
    was_busy_at_pause: bool | None = None
    overridden_instances: tuple[str, ...] = ()
    pending_override_instances: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Any) -> Supervision:
        if not isinstance(d, Mapping):
            return cls()
        state = d.get("state")
        busy = d.get("was_busy_at_pause")
        return cls(
            state=state if state in STATES else RUNNING,
            holds=tuple(_str_list(d.get("holds"))),
            paused_at=parse_time(d.get("paused_at")),
            resume_at=parse_time(d.get("resume_at")),
            was_busy_at_pause=busy if isinstance(busy, bool) else None,
            overridden_instances=tuple(_str_list(d.get("overridden_instances"))),
            pending_override_instances=tuple(_str_list(d.get("pending_override_instances"))),
        )


@dataclass(frozen=True)
class SessionView:
    """What the policy needs from one `sessions/<wrapper_id>.json` record."""

    wrapper_id: str
    model_id: str | None = None
    activity: str = "unknown"
    supervision: Supervision = field(default_factory=Supervision)

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> SessionView | None:
        wrapper_id = _str(rec.get("wrapper_id"))
        if wrapper_id is None:
            return None
        return cls(
            wrapper_id=wrapper_id,
            model_id=_str(rec.get("model_id")),
            activity=_str(rec.get("activity")) or "unknown",
            supervision=Supervision.from_dict(rec.get("supervision")),
        )
