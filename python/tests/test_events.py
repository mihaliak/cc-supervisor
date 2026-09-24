"""EventBus: dedupe (persisted), rotation, notify toggles, title/body, subscribers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ccs.clock import FakeClock
from ccs.config.models import Config
from ccs.events import (
    Event,
    EventBus,
    hour_bucket,
    notification_text,
    read_tail,
    register_text,
)


def cfg(**notifications: bool) -> Config:
    raw: dict[str, Any] = {
        "version": 1,
        "revision": 0,
        "default_profile": "work",
        "profiles": [
            {"id": "work", "flag": "work", "name": "Work", "emoji": "💼", "config_dir": "~/x"}
        ],
    }
    if notifications:
        base = {"limit_warn": True, "limit_pause": True, "limit_resume": True}
        base.update({"warmup": True, "errors": True})
        base.update(notifications)
        raw["notifications"] = base
    return Config.from_dict(raw)


def make_bus(tmp_path: Path, config: Config | None = None, **kw: Any) -> EventBus:
    return EventBus(
        clock=FakeClock(),
        config=lambda: config,
        events_path=tmp_path / "events.jsonl",
        seen_path=tmp_path / "events.seen.json",
        **kw,
    )


def lines(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / "events.jsonl").read_text("utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_keyed_event_is_deduped(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg())
    assert bus.emit(Event("limit.warn", "work", "warn:session:x", {})) is not None
    assert bus.emit(Event("limit.warn", "work", "warn:session:x", {})) is None
    assert len(lines(tmp_path)) == 1


def test_unkeyed_events_are_never_deduped(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg())
    bus.emit(Event("session.started", "work"))
    bus.emit(Event("session.started", "work"))
    assert len(lines(tmp_path)) == 2


def test_dedupe_survives_restart(tmp_path: Path) -> None:
    make_bus(tmp_path, cfg()).emit(Event("auth.required", "work", "auth:work:2026092412", {}))
    again = make_bus(tmp_path, cfg())
    assert again.emit(Event("auth.required", "work", "auth:work:2026092412", {})) is None
    assert again.emit(Event("auth.required", "work", "auth:work:2026092413", {})) is not None


def test_dedupe_horizon_expires(tmp_path: Path) -> None:
    clock = FakeClock()
    bus = EventBus(
        clock=clock,
        config=lambda: None,
        events_path=tmp_path / "e.jsonl",
        seen_path=tmp_path / "s.json",
    )
    bus.emit(Event("limit.warn", "work", "k", {}))
    clock.advance(timedelta(days=9).total_seconds())
    assert bus.emit(Event("limit.warn", "work", "k", {})) is not None


def test_rotation_keeps_two_files(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg(), rotate_bytes=300)
    for i in range(40):
        bus.emit(Event("session.started", "work", None, {"i": i}))
    assert (tmp_path / "events.jsonl").exists()
    assert (tmp_path / "events.jsonl.1").exists()
    assert (tmp_path / "events.jsonl.2").exists()
    assert not (tmp_path / "events.jsonl.3").exists()
    assert (tmp_path / "events.jsonl").stat().st_size < 600


def test_notify_follows_toggles(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg(limit_warn=False, errors=True))
    warn = bus.emit(Event("limit.warn", "work", "a", {}))
    auth = bus.emit(Event("auth.required", "work", "b", {}))
    started = bus.emit(Event("session.started", "work", None, {}))
    assert warn is not None and auth is not None and started is not None
    assert warn["data"]["notify"] is False
    assert auth["data"]["notify"] is True
    assert started["data"]["notify"] is False  # never-notified type


def test_notify_false_without_config(tmp_path: Path) -> None:
    record = make_bus(tmp_path, None).emit(Event("auth.required", "work", "k", {}))
    assert record is not None and record["data"]["notify"] is False


def test_emitters_cannot_set_notification_fields(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg(errors=False))
    record = bus.emit(Event("auth.required", "work", "k", {"notify": True, "title": "x"}))
    assert record is not None
    assert record["data"]["notify"] is False
    assert record["data"]["title"] == "💼 Work: sign in required"


def test_title_body_in_log_and_push(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg())
    pushed: list[dict[str, Any]] = []
    unsubscribe = bus.subscribe(pushed.append)
    bus.emit(Event("usage.source_error", "work", "k", {"error": "boom"}))
    unsubscribe()
    bus.emit(Event("usage.source_error", "work", "k2", {"error": "boom"}))
    logged = lines(tmp_path)[0]
    assert logged["data"]["title"] == "💼 Work: usage unavailable"
    assert logged["data"]["body"] == "boom"
    assert pushed == [logged]
    assert logged["schema"] == 1 and logged["ts"].endswith("Z")


def test_subscriber_errors_do_not_break_emit(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg())

    def broken(_: dict[str, Any]) -> None:
        raise RuntimeError("x")

    bus.subscribe(broken)
    assert bus.emit(Event("session.started", "work")) is not None


def test_notification_text_fallback_and_auth() -> None:
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    profile = cfg().profile("work")
    assert notification_text("daemon.started", None, {}, now) == (
        "CC Supervisor: daemon.started",
        "",
    )
    title, body = notification_text("auth.required", profile, {}, now)
    assert title == "💼 Work: sign in required"
    assert body == (
        "Open CC Supervisor → Settings → Profiles → Work → Sign in, "
        "or run: ccs auth login --profile work"
    )


def test_register_text_overrides_template(tmp_path: Path) -> None:
    register_text("warmup.failed", lambda p, d, now: ("T", f"B {d.get('reason')}"))
    try:
        record = make_bus(tmp_path, cfg()).emit(
            Event("warmup.failed", "work", None, {"reason": "x"})
        )
        assert record is not None
        assert (record["data"]["title"], record["data"]["body"]) == ("T", "B x")
    finally:
        from ccs import events

        events._TEXT_BUILDERS.pop("warmup.failed", None)


def test_hour_bucket_uses_given_zone() -> None:
    now = datetime(2026, 9, 24, 23, 30, tzinfo=UTC)
    assert hour_bucket(now, UTC) == "2026092423"


def test_read_tail(tmp_path: Path) -> None:
    bus = make_bus(tmp_path, cfg())
    for i in range(60):
        bus.emit(Event("session.started", "work", None, {"i": i}))
    tail = read_tail(tmp_path / "events.jsonl", limit=50)
    assert len(tail) == 50
    assert tail[-1]["data"]["i"] == 59
    assert read_tail(tmp_path / "missing.jsonl") == []
