"""Golden notification text for every notified event type (ADR-0015, P06 templates)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ccs.config.models import Profile
from ccs.events import notification_text

NOW = datetime(2026, 9, 24, 16, 48, tzinfo=UTC)  # 18:48 in Europe/Bratislava (CEST)


@pytest.fixture(autouse=True)
def _tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Bratislava")


def work() -> Profile:
    return Profile.from_dict(
        {"id": "work", "flag": "work", "name": "Work", "emoji": "💼", "config_dir": "/tmp/w"}
    )


def text(etype: str, data: dict[str, Any]) -> tuple[str, str]:
    return notification_text(etype, work(), data, NOW)


@pytest.mark.parametrize(
    ("etype", "data", "title", "body"),
    [
        (
            "limit.warn",
            {"window": "session", "percent": 82, "resets_at": "2026-09-24T18:00:00Z"},
            "💼 Work: session at 82%",
            "Resets 20:00 (in 1h 12m)",
        ),
        (
            "limit.warn",
            {"window": "weekly", "percent": 86, "resets_at": "2026-09-26T06:00:00Z"},
            "💼 Work: weekly limit at 86%",
            "Resets Sat 08:00 (in 1d 13h)",
        ),
        (
            "limit.warn",
            {
                "window": "model_scoped",
                "name": "Fable",
                "percent": 82,
                "resets_at": "2026-09-26T06:00:00Z",
                "level": "warn",
            },
            "💼 Work: Fable weekly limit at 82%",
            "Resets Sat 08:00 (in 1d 13h)",
        ),
        (
            "limit.warn",
            {
                "window": "model_scoped",
                "name": "Fable",
                "percent": 95,
                "resets_at": "2026-09-26T06:00:00Z",
                "level": "pause_level",
            },
            "💼 Work: Fable at 95% (not pausing)",
            "Resets Sat 08:00 (in 1d 13h)",
        ),
        (
            "limit.warn",
            {
                "window": "extra_usage",
                "percent": 80,
                "used": 8.0,
                "limit": 10.0,
                "currency": "EUR",
            },
            "💼 Work: extra usage at 80% (€8.00 / €10.00)",
            "Monthly credit cap",
        ),
        (
            "limit.pause",
            {
                "window": "session",
                "percent": 90,
                "resume_at": "2026-09-24T17:30:00Z",
                "sessions_paused": 2,
            },
            "💼 Work paused at 90%",
            "2 sessions paused. Resumes 19:30 (in 42m)",
        ),
        (
            "limit.pause",
            {
                "window": "session",
                "percent": 91,
                "resume_at": "2026-09-24T17:30:00Z",
                "sessions_paused": 0,
            },
            "💼 Work paused at 91%",
            "New ccs sessions will ask before starting. Resumes 19:30 (in 42m)",
        ),
        (
            "limit.pause",
            {
                "window": "weekly",
                "percent": 95,
                "resume_at": "2026-09-26T06:00:00Z",
                "sessions_paused": 1,
            },
            "💼 Work paused at 95% (weekly limit)",
            "1 session paused. Resumes Sat 08:00 (in 1d 13h)",
        ),
        (
            "limit.pause",
            {"window": "extra_usage", "percent": 90, "resume_at": None, "sessions_paused": 1},
            "💼 Work paused at 90% (extra usage)",
            "1 session paused. Resumes when credits allow",
        ),
        (
            "limit.pause",
            {"window": "manual", "manual": True, "sessions_paused": 3},
            "💼 Work paused manually",
            "3 sessions paused. Resume with: ccs resume --profile work",
        ),
        (
            "limit.resume",
            {"window": "session", "sessions_resumed": 2},
            "💼 Work resumed",
            "2 sessions continued",
        ),
        (
            "limit.resume",
            {"window": "session", "sessions_resumed": 0},
            "💼 Work resumed",
            "Limit reset; new sessions can start",
        ),
        (
            "limit.resume",
            {"window": "manual", "manual": True, "sessions_resumed": 0},
            "💼 Work resumed",
            "Resumed manually",
        ),
        (
            "warmup.succeeded",
            {"trigger": "unlock_wake", "resets_at": "2026-09-24T21:48:00Z"},
            "💼 Work: session window started",
            "Resets 23:48 (in 5h)",
        ),
        (
            "warmup.failed",
            {"trigger": "schedule", "reason": "window_not_started"},
            "💼 Work: warm-up failed",
            "the session window did not start",
        ),
        (
            "warmup.failed",
            {"trigger": "manual", "reason": "exit_1", "detail": "boom"},
            "💼 Work: warm-up failed",
            "claude exited with code 1: boom",
        ),
        (
            "auth.required",
            {},
            "💼 Work: sign in required",
            "Open CC Supervisor → Settings → Profiles → Work → Sign in, "
            "or run: ccs auth login --profile work",
        ),
    ],
)
def test_golden(etype: str, data: dict[str, Any], title: str, body: str) -> None:
    assert text(etype, data) == (title, body)


def test_unknown_type_falls_back() -> None:
    assert text("session.started", {}) == ("💼 Work: session.started", "")


def test_broken_data_never_raises() -> None:
    title, _ = text("limit.pause", {"sessions_paused": "x", "resume_at": 5})
    assert title.startswith("💼 Work paused")
