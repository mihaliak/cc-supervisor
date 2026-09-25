"""Events: model, notification fields, dedupe, `events.jsonl` log and subscribers (ADR-0005/0015).

`EventBus.emit` is the only place notification fields are computed. Emitters build an
`Event(type, profile_id, key, data)` and never set `title`, `body` or `notify` themselves.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ccs import fsio, paths
from ccs.clock import Clock, SystemClock, local_tz
from ccs.config.models import Config, Profile
from ccs.usage.model import format_iso, parse_time

log = logging.getLogger(__name__)

SCHEMA = 1
ROTATE_BYTES = 5 * 1024 * 1024
ROTATE_KEEP = 2
SEEN_MAX = 1000
SEEN_HORIZON = timedelta(days=8)  # longer than a weekly window, so instance keys never repeat

# Event type → `config.notifications` toggle (ADR-0015). Other types are never notified.
TOGGLES: dict[str, str] = {
    "limit.warn": "limit_warn",
    "limit.pause": "limit_pause",
    "limit.resume": "limit_resume",
    "warmup.succeeded": "warmup",
    "warmup.failed": "warmup",
    "auth.required": "errors",
    "usage.source_error": "errors",
    "config.invalid": "errors",
}
NOTIFIED_TYPES = frozenset(TOGGLES)
# Sent on request (Settings → Notifications → Send Test Notification); ignores the toggles.
TEST_TYPE = "notify.test"

TYPES = frozenset(
    {
        TEST_TYPE,
        "limit.warn",
        "limit.pause",
        "limit.resume",
        "limit.override",
        "warmup.started",
        "warmup.skipped",
        "warmup.succeeded",
        "warmup.failed",
        "auth.required",
        "usage.source_error",
        "config.invalid",
        "session.started",
        "session.ended",
        "daemon.started",
        "daemon.stopped",
    }
)


@dataclass(frozen=True)
class Event:
    """One event. `key` is the dedupe key (`None` = never deduped)."""

    type: str
    profile_id: str | None = None
    key: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "ts": format_iso(self.ts),
            "type": self.type,
            "profile_id": self.profile_id,
            "key": self.key,
            "data": copy.deepcopy(self.data),
        }

    @classmethod
    def from_dict(cls, d: Any) -> Event | None:
        """Tolerant parse of one logged/pushed event."""
        if not isinstance(d, dict) or not isinstance(d.get("type"), str):
            return None
        pid = d.get("profile_id")
        key = d.get("key")
        data = d.get("data")
        return cls(
            type=d["type"],
            profile_id=pid if isinstance(pid, str) else None,
            key=key if isinstance(key, str) else None,
            data=data if isinstance(data, dict) else {},
            ts=parse_time(d.get("ts")),
        )


def hour_bucket(now: datetime, tz: tzinfo | None = None) -> str:
    """`YYYYmmddHH` in local time: error keys carry it so they notify ≤ once per hour."""
    return now.astimezone(tz or local_tz()).strftime("%Y%m%d%H")


# ---------------------------------------------------------------- notification text

TextBuilder = Callable[[Profile | None, dict[str, Any], datetime], tuple[str, str]]
_TEXT_BUILDERS: dict[str, TextBuilder] = {}


def register_text(event_type: str, builder: TextBuilder) -> None:
    """Install the title/body template for `event_type` (P06 registers the real templates)."""
    _TEXT_BUILDERS[event_type] = builder


def _prefix(profile: Profile | None) -> str:
    if profile is None:
        return "CC Supervisor"
    return f"{profile.emoji} {profile.name}".strip()


def _auth_required(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    if profile is None:
        return "CC Supervisor: sign in required", ""
    body = (
        f"Open CC Supervisor → Settings → Profiles → {profile.name} → Sign in, "
        f"or run: ccs auth login --profile {profile.id}"
    )
    return f"{_prefix(profile)}: sign in required", body


def _source_error(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    error = data.get("error")
    return f"{_prefix(profile)}: usage unavailable", str(error) if error else ""


def _config_invalid(
    profile: Profile | None, data: dict[str, Any], now: datetime
) -> tuple[str, str]:
    issues = data.get("issues")
    first = ""
    if isinstance(issues, list) and issues and isinstance(issues[0], dict):
        issue = issues[0]
        first = f"{issue.get('path') or '<root>'}: {issue.get('message')}"
    return "CC Supervisor: config invalid", first or "Run: ccs config validate"


def _when(value: Any, now: datetime) -> str | None:
    """`20:00 (in 1h 12m)` for an ISO timestamp (ADR-0009), or `None`."""
    from ccs.timefmt import format_reset_combined

    when = parse_time(value)
    return format_reset_combined(when, now, local_tz()) if when is not None else None


def _money(value: Any, currency: Any) -> str:
    from ccs.snapshot import format_money

    amount = float(value) if isinstance(value, (int, float)) else None
    return format_money(amount, currency if isinstance(currency, str) else None)


def _sessions(n: int, verb: str) -> str:
    return f"{n} session{'s' if n != 1 else ''} {verb}"


def _window_label(data: dict[str, Any]) -> str:
    window = data.get("window")
    name = data.get("name")
    if window == "weekly":
        return "weekly limit"
    if window == "model_scoped":
        return f"{name} weekly limit" if name else "model weekly limit"
    if window == "extra_usage":
        return "extra usage"
    return "session"


def _limit_warn(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    percent = data.get("percent")
    window = data.get("window")
    name = data.get("name")
    if window == "extra_usage":
        spend = f" ({_money(data.get('used'), data.get('currency'))} / "
        spend += f"{_money(data.get('limit'), data.get('currency'))})"
        return f"{_prefix(profile)}: extra usage at {percent}%{spend}", "Monthly credit cap"
    if data.get("level") == "pause_level":
        label = name or "model"
        title = f"{_prefix(profile)}: {label} at {percent}% (not pausing)"
    else:
        title = f"{_prefix(profile)}: {_window_label(data)} at {percent}%"
    when = _when(data.get("resets_at"), now)
    return title, f"Resets {when}" if when else ""


def _limit_pause(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    window = data.get("window")
    raw_n = data.get("sessions_paused")
    n = raw_n if isinstance(raw_n, int) else 0
    if window == "manual":
        title = f"{_prefix(profile)} paused manually"
    else:
        title = f"{_prefix(profile)} paused at {data.get('percent')}%"
        if window != "session":
            title += f" ({_window_label(data)})"
    lead = _sessions(n, "paused") + "." if n else "New ccs sessions will ask before starting."
    if window == "manual":
        pid = profile.id if profile is not None else "<id>"
        return title, f"{lead} Resume with: ccs resume --profile {pid}"
    when = _when(data.get("resume_at"), now)
    tail = f"Resumes {when}" if when else "Resumes when credits allow"
    return title, f"{lead} {tail}"


def _limit_resume(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    raw_n = data.get("sessions_resumed")
    n = raw_n if isinstance(raw_n, int) else 0
    if n:
        body = _sessions(n, "continued")
    elif data.get("manual"):
        body = "Resumed manually"
    else:
        body = "Limit reset; new sessions can start"
    return f"{_prefix(profile)} resumed", body


_WARMUP_REASONS = {
    "timeout": "claude did not answer within the timeout",
    "window_not_started": "the session window did not start",
    "claude_not_found": "claude was not found",
    "spawn_failed": "claude could not be started",
    "error": "unexpected error",
}


def _warmup_succeeded(
    profile: Profile | None, data: dict[str, Any], now: datetime
) -> tuple[str, str]:
    when = _when(data.get("resets_at"), now)
    return f"{_prefix(profile)}: session window started", f"Resets {when}" if when else ""


def _warmup_failed(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    reason = data.get("reason")
    text = _WARMUP_REASONS.get(str(reason), "")
    if not text and isinstance(reason, str) and reason.startswith("exit_"):
        text = f"claude exited with code {reason[5:]}"
    detail = data.get("detail")
    body = text or str(reason or "")
    if isinstance(detail, str) and detail:
        body = f"{body}: {detail}" if body else detail
    return f"{_prefix(profile)}: warm-up failed", body


def _notify_test(profile: Profile | None, data: dict[str, Any], now: datetime) -> tuple[str, str]:
    return "CC Supervisor", "Test notification: notifications are working."


register_text(TEST_TYPE, _notify_test)
register_text("auth.required", _auth_required)
register_text("usage.source_error", _source_error)
register_text("config.invalid", _config_invalid)
register_text("limit.warn", _limit_warn)
register_text("limit.pause", _limit_pause)
register_text("limit.resume", _limit_resume)
register_text("warmup.succeeded", _warmup_succeeded)
register_text("warmup.failed", _warmup_failed)


def notification_text(
    event_type: str, profile: Profile | None, data: dict[str, Any], now: datetime
) -> tuple[str, str]:
    """`(title, body)` for an event. Registered templates win; else a minimal fallback."""
    builder = _TEXT_BUILDERS.get(event_type)
    if builder is not None:
        try:
            return builder(profile, data, now)
        except Exception:  # a broken template must never break event delivery
            log.exception("notification template for %s failed", event_type)
    return f"{_prefix(profile)}: {event_type}", ""


# ---------------------------------------------------------------- bus

Subscriber = Callable[[dict[str, Any]], None]


class EventBus:
    """Dedupe → text → toggles → `notify` → append → push (ADR-0015)."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        config: Callable[[], Config | None] | None = None,
        events_path: Path | None = None,
        seen_path: Path | None = None,
        rotate_bytes: int = ROTATE_BYTES,
    ) -> None:
        self.clock = clock or SystemClock()
        self._config = config or (lambda: None)
        self.events_path = Path(events_path) if events_path else paths.events_file()
        self.seen_path = Path(seen_path) if seen_path else paths.events_seen_file()
        self.rotate_bytes = rotate_bytes
        self._subscribers: list[Subscriber] = []
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._load_seen()

    # -- dedupe memory

    def _load_seen(self) -> None:
        data = fsio.read_json(self.seen_path)
        keys = data.get("keys") if data else None
        if not isinstance(keys, dict):
            return
        items: list[tuple[str, datetime]] = []
        for key, ts in keys.items():
            when = parse_time(ts)
            if isinstance(key, str) and when is not None:
                items.append((key, when))
        for key, when in sorted(items, key=lambda kv: kv[1])[-SEEN_MAX:]:
            self._seen[key] = when

    def _save_seen(self) -> None:
        payload = {"schema": SCHEMA, "keys": {k: format_iso(v) for k, v in self._seen.items()}}
        try:
            fsio.atomic_write_json(self.seen_path, payload)
        except (OSError, ValueError) as exc:
            log.warning("cannot persist event dedupe keys: %s", exc)

    def seen(self, key: str, now: datetime) -> bool:
        """Whether `key` was emitted within the dedupe horizon."""
        when = self._seen.get(key)
        return when is not None and now - when <= SEEN_HORIZON

    def _remember(self, key: str, now: datetime) -> None:
        self._seen[key] = now
        self._seen.move_to_end(key)
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        self._save_seen()

    # -- subscribers

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Receive every emitted event dict. Returns an unsubscribe function."""
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(fn)

        return unsubscribe

    # -- log

    def _rotate_if_needed(self) -> None:
        try:
            size = self.events_path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.rotate_bytes:
            return
        for i in range(ROTATE_KEEP, 0, -1):
            src = self.events_path if i == 1 else Path(f"{self.events_path}.{i - 1}")
            dst = Path(f"{self.events_path}.{i}")
            if src.exists():
                os.replace(src, dst)

    def _append(self, record: dict[str, Any]) -> bool:
        """Log one record; False when it could not be written (IO or encoding)."""
        try:
            self._rotate_if_needed()
            fsio.append_jsonl(self.events_path, record)
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            log.warning("cannot append event %s: %s", record.get("type"), exc)
            return False
        return True

    # -- emit

    def emit(self, event: Event) -> dict[str, Any] | None:
        """Process one event; returns the final dict, or `None` when deduped.

        The dedupe key is remembered only once the record is logged, so an event that failed
        to append is not silently suppressed next time.
        """
        now = self.clock.now()
        if event.key is not None and self.seen(event.key, now):
            log.debug("event %s deduped (%s)", event.type, event.key)
            return None
        cfg = self._config()
        profile = cfg.profile(event.profile_id) if cfg and event.profile_id else None
        data = fsio.scrub_surrogates(copy.deepcopy(event.data))
        for reserved in ("title", "body", "notify"):
            data.pop(reserved, None)
        title, body = notification_text(event.type, profile, data, now)
        data["title"] = title
        data["body"] = body
        toggle = TOGGLES.get(event.type)
        enabled = event.type == TEST_TYPE or bool(
            cfg is not None and toggle and getattr(cfg.notifications, toggle, False)
        )
        data["notify"] = enabled
        record = Event(event.type, event.profile_id, event.key, data, event.ts or now).to_dict()
        if self._append(record) and event.key is not None:
            self._remember(event.key, now)
        for fn in list(self._subscribers):
            try:
                fn(copy.deepcopy(record))
            except Exception:
                log.exception("event subscriber failed")
        return record


def read_tail(path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """The last `limit` parseable events of `events.jsonl` (tolerant)."""
    p = Path(path) if path else paths.events_file()
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            chunk = min(size, max(64 * 1024, limit * 2048))
            fh.seek(size - chunk)
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if chunk < size and lines:
        lines = lines[1:]  # the first line may be partial
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out[-limit:]
