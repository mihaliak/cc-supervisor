"""Golden tests for `ccs.statusline.render` (ADR-0009 exact strings and colors)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sl_helpers import NOW, SESSION_RESET, TZ, WEEKLY_RESET, iso, usage_doc

from ccs.statusline.render import (
    GRAY,
    GREEN,
    PLAIN,
    RED,
    YELLOW,
    Limits,
    RenderContext,
    Segment,
    bar,
    currency_symbol,
    display_percent,
    fallback_line,
    level_for,
    parse_time,
    render,
    to_ansi,
    to_plain,
)

HEAD = "💼 Work ~ project ~ Opus 5.5 / xhigh ~ "
TIME_VECTORS = Path(__file__).resolve().parents[3] / "schema" / "fixtures" / "time_format.json"


def stdin(
    session: float | None = None,
    session_reset: datetime | None = SESSION_RESET,
    weekly: float | None = None,
    weekly_reset: datetime | None = WEEKLY_RESET,
    *,
    model: str | None = "Opus 5.5",
    effort: str | None = "xhigh",
    folder: str | None = "/Users/you/Code/project",
) -> dict[str, Any]:
    data: dict[str, Any] = {"session_id": "s1"}
    if model is not None:
        data["model"] = {"id": "claude-opus-5-5", "display_name": model}
    if effort is not None:
        data["effort"] = {"level": effort}
    if folder is not None:
        data["workspace"] = {"current_dir": folder}
    limits: dict[str, Any] = {}
    if session is not None:
        entry: dict[str, Any] = {"used_percentage": session}
        if session_reset is not None:
            entry["resets_at"] = int(session_reset.timestamp())
        limits["five_hour"] = entry
    if weekly is not None:
        entry = {"used_percentage": weekly}
        if weekly_reset is not None:
            entry["resets_at"] = int(weekly_reset.timestamp())
        limits["seven_day"] = entry
    if limits:
        data["rate_limits"] = limits
    return data


def ctx(
    data: dict[str, Any] | None = None,
    *,
    now: datetime = NOW,
    usage: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
    wrapper_id: str | None = None,
    offline: bool = False,
    emoji: str = "💼",
    limits: Limits | None = None,
) -> RenderContext:
    return RenderContext(
        profile_id="work",
        name="Work",
        emoji=emoji,
        limits=limits or Limits(),
        now=now,
        tz=TZ,
        stdin=data if data is not None else stdin(),
        usage=usage,
        session_record=record,
        wrapper_id=wrapper_id if wrapper_id is not None else ("w1" if record else None),
        supervisor_offline=offline,
    )


def plain(context: RenderContext) -> str:
    return to_plain(render(context))


def supervision(state: str, resume_at: datetime | None, holds: list[str]) -> dict[str, Any]:
    return {
        "wrapper_id": "w1",
        "supervision": {
            "state": state,
            "holds": holds,
            "resume_at": iso(resume_at) if resume_at else None,
            "paused_at": None,
            "was_busy_at_pause": True,
        },
    }


# ---------------------------------------------------------------- session states (exact)

LATE = SESSION_RESET - timedelta(minutes=42)  # 17:18Z → "in 42m"


def test_normal_exact() -> None:
    line = plain(ctx(stdin(45)))
    assert line == HEAD + "45% ▓▓▓▓▓░░░░░ 20:00 (in 2h 13m)"


def test_warn_exact() -> None:
    line = plain(ctx(stdin(84), now=LATE))
    assert line == HEAD + "⚠ 84% ▓▓▓▓▓▓▓▓░░ 20:00 (in 42m) · limit approaching"


def test_paused_exact() -> None:
    rec = supervision("paused", SESSION_RESET, ["session"])
    line = plain(ctx(stdin(90), now=LATE, record=rec))
    assert line == HEAD + "⏸ 90% ▓▓▓▓▓▓▓▓▓░ paused → resumes 20:00 (in 42m)"


def test_paused_uses_hold_resume_time() -> None:
    rec = supervision("paused", WEEKLY_RESET, ["weekly"])
    line = plain(ctx(stdin(45, weekly=96), record=rec))
    assert "⏸ 45% ▓▓▓▓▓░░░░░ paused → resumes Sat 08:00 (in 1d 14h)" in line
    assert line.endswith(" ~ W ⚠ 96% Sat 08:00")


def test_paused_manual_exact() -> None:
    rec = supervision("paused", None, ["manual"])
    line = plain(ctx(stdin(45), record=rec))
    assert line == HEAD + "⏸ 45% ▓▓▓▓▓░░░░░ paused (manual)"


def test_paused_by_credit_cap_says_credits() -> None:
    line = plain(ctx(stdin(45), record=supervision("paused", None, ["extra_usage"])))
    assert "⏸ 45% ▓▓▓▓▓░░░░░ paused (credits)" in line
    both = supervision("paused", None, ["extra_usage", "manual"])
    assert "paused (manual)" in plain(ctx(stdin(45), record=both))


def test_overridden_exact() -> None:
    rec = supervision("overridden", SESSION_RESET, ["session"])
    line = plain(ctx(stdin(91), now=LATE, record=rec))
    assert line == HEAD + "⚠ 91% ▓▓▓▓▓▓▓▓▓░ 20:00 (in 42m) · override"


def test_no_data_exact() -> None:
    line = plain(ctx(stdin()))
    assert line == HEAD + "?% ░░░░░░░░░░"
    segments = render(ctx(stdin()))
    assert segments[-1] == Segment("?% ░░░░░░░░░░", GRAY)


def test_paused_without_data() -> None:
    rec = supervision("paused", SESSION_RESET, ["session"])
    line = plain(ctx(stdin(), now=LATE, record=rec))
    assert line == HEAD + "⏸ ?% ░░░░░░░░░░ paused → resumes 20:00 (in 42m)"


def test_normal_segment_colors() -> None:
    segments = render(ctx(stdin(45)))
    assert segments == [
        Segment("💼 Work", PLAIN),
        Segment(" ~ ", GRAY),
        Segment("project", PLAIN),
        Segment(" ~ ", GRAY),
        Segment("Opus 5.5 / xhigh", PLAIN),
        Segment(" ~ ", GRAY),
        Segment("45% ▓▓▓▓▓░░░░░", GREEN),
        Segment(" 20:00 (in 2h 13m)", PLAIN),
    ]


def test_warn_segment_colors() -> None:
    segments = render(ctx(stdin(84), now=LATE))
    assert segments[-2:] == [
        Segment("⚠ 84% ▓▓▓▓▓▓▓▓░░", RED),
        Segment(" 20:00 (in 42m) · limit approaching", PLAIN),
    ]


# ---------------------------------------------------------------- percent table

PERCENTS = [
    # percent, core text, color
    (0, "0% ░░░░░░░░░░", GREEN),
    (5, "5% ▓░░░░░░░░░", GREEN),
    (45, "45% ▓▓▓▓▓░░░░░", GREEN),
    (50, "50% ▓▓▓▓▓░░░░░", YELLOW),
    (79, "79% ▓▓▓▓▓▓▓▓░░", YELLOW),
    (80, "⚠ 80% ▓▓▓▓▓▓▓▓░░", RED),
    (84, "⚠ 84% ▓▓▓▓▓▓▓▓░░", RED),
    (90, "⚠ 90% ▓▓▓▓▓▓▓▓▓░", RED),
    (100, "⚠ 100% ▓▓▓▓▓▓▓▓▓▓", RED),
    (117, "⚠ 117% ▓▓▓▓▓▓▓▓▓▓", RED),
]


@pytest.mark.parametrize(("percent", "core", "color"), PERCENTS)
@pytest.mark.parametrize("source", ["stdin", "file", "both"])
def test_percent_table(percent: int, core: str, color: str, source: str) -> None:
    usage = usage_doc(session=(percent, SESSION_RESET)) if source != "stdin" else None
    if source == "both":
        usage = usage_doc(session=(3, SESSION_RESET))  # stdin must win
    data = stdin(percent if source != "file" else None)
    segments = render(ctx(data, usage=usage))
    session = [s for s in segments if "▓" in s.text or "░" in s.text]
    assert session == [Segment(core, color)]


def test_neither_source_is_no_data() -> None:
    assert plain(ctx(stdin(), usage=None)).endswith("?% ░░░░░░░░░░")


def test_float_stdin_rounds_half_up() -> None:
    assert "45% " in plain(ctx(stdin(44.5)))
    assert "44% " in plain(ctx(stdin(44.49)))


# ---------------------------------------------------------------- base line variants


def test_effort_absent_drops_suffix() -> None:
    line = plain(ctx(stdin(45, effort=None)))
    assert line.startswith("💼 Work ~ project ~ Opus 5.5 ~ 45% ")


def test_model_absent_drops_part() -> None:
    line = plain(ctx(stdin(45, model=None)))
    assert line.startswith("💼 Work ~ project ~ 45% ")


def test_empty_emoji_drops_prefix() -> None:
    line = plain(ctx(stdin(45), emoji=""))
    assert line.startswith("Work ~ project ~ Opus 5.5 / xhigh ~ 45%")


def test_folder_falls_back_to_cwd() -> None:
    data = stdin(45, folder=None)
    data["cwd"] = "/tmp/elsewhere/"
    assert plain(ctx(data)).startswith("💼 Work ~ elsewhere ~ ")


def test_fixture_stdin_haiku_no_effort() -> None:
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/statusline/with_rate_limits.json"
        ).read_text()
    )
    reset_now = datetime.fromtimestamp(fixture["rate_limits"]["five_hour"]["resets_at"], TZ)
    line = plain(ctx(fixture, now=reset_now - timedelta(hours=1)))
    assert line.startswith("💼 Work ~ cc-supervisor ~ Haiku 4.5 ~ 18% ▓▓░░░░░░░░ ")
    assert "(in 1h)" in line


def test_no_reset_time_omits_time() -> None:
    line = plain(ctx(stdin(45, session_reset=None)))
    assert line == HEAD + "45% ▓▓▓▓▓░░░░░"


CONTROLS = [chr(c) for c in (*range(0x20), *range(0x7F, 0xA0))]


def test_stdin_control_chars_are_stripped() -> None:
    data = stdin(45, model="Opus\x1b[2J 5.5", effort="x\x9bhigh", folder="/tmp/evil\x1b]0;t\x07\nx")
    usage = usage_doc(model_scoped=[("Fa\x1b[31mble", 82, WEEKLY_RESET)])
    usage["extra_usage"] = {"enabled": True, "percent": 90, "used": 1, "currency": "X\rY"}
    context = ctx(data, usage=usage, limits=Limits(spill=True))
    line = plain(context)
    assert not any(ch in line for ch in CONTROLS)
    assert line.startswith("💼 Work ~ evil]0;tx ~ Opus[2J 5.5 / xhigh ~ 45% ")
    assert "~ Fa[31mble ⚠ 82% Sat 08:00" in line
    assert "~ XY 1.00" in line
    assert "\x1b]" not in to_ansi(render(context))


def test_only_control_chars_drop_the_part() -> None:
    line = plain(ctx(stdin(45, model="\x1b\x07", folder="\x1b\n")))
    assert line.startswith("💼 Work ~ 45% ")


@pytest.mark.parametrize(
    "value",
    [
        "9999-12-31T23:59:59+00:00",  # tz shift / minute rounding would overflow
        "0001-01-01T00:00:00+05:00",  # before year 1 in UTC
        253402300000,  # epoch seconds in the last hours of year 9999
        1790000000000,  # epoch milliseconds: out of range
        1e300,
        float("nan"),
        "2026-13-01T00:00:00Z",
    ],
)
def test_unusable_reset_time_skips_the_time(value: Any) -> None:
    assert parse_time(value) is None
    data = stdin(40, session_reset=None, weekly=90, weekly_reset=None)
    data["rate_limits"]["five_hour"]["resets_at"] = value
    data["rate_limits"]["seven_day"]["resets_at"] = value
    assert plain(ctx(data)) == HEAD + "40% ▓▓▓▓░░░░░░ ~ W ⚠ 90%"


# ---------------------------------------------------------------- data precedence / freshness


def test_file_used_without_stdin() -> None:
    usage = usage_doc(session=(62, SESSION_RESET))
    assert plain(ctx(stdin(), usage=usage)) == HEAD + "62% ▓▓▓▓▓▓░░░░ 20:00 (in 2h 13m)"


def test_stale_file_is_no_data() -> None:
    usage = usage_doc(session=(62, SESSION_RESET), fetched_at=NOW - timedelta(minutes=11))
    assert plain(ctx(stdin(), usage=usage)).endswith("?% ░░░░░░░░░░")


@pytest.mark.parametrize("status", ["stale", "needs_sign_in", "source_error", "no_subscription"])
def test_non_ok_file_is_no_data(status: str) -> None:
    usage = usage_doc(status=status, session=(62, SESSION_RESET))
    assert plain(ctx(stdin(), usage=usage)).endswith("?% ░░░░░░░░░░")


def test_window_past_reset_is_no_data() -> None:
    past = NOW - timedelta(minutes=1)
    assert plain(ctx(stdin(45, session_reset=past))).endswith("?% ░░░░░░░░░░")
    usage = usage_doc(session=(62, past))
    assert plain(ctx(stdin(), usage=usage)).endswith("?% ░░░░░░░░░░")


# ---------------------------------------------------------------- extra segments


@pytest.mark.parametrize(
    ("weekly", "holds", "shown"), [(79, [], False), (80, [], True), (50, ["weekly"], True)]
)
def test_weekly_visibility(weekly: int, holds: list[str], shown: bool) -> None:
    rec = supervision("paused", WEEKLY_RESET, holds) if holds else None
    line = plain(ctx(stdin(45, weekly=weekly), record=rec))
    assert (f" ~ W ⚠ {weekly}% Sat 08:00" in line) is shown
    if not holds:
        assert (line.endswith(f" ~ W ⚠ {weekly}% Sat 08:00")) is shown


def test_weekly_from_file_colors() -> None:
    usage = usage_doc(weekly=(86, WEEKLY_RESET))
    segments = render(ctx(stdin(45), usage=usage))
    assert segments[-2:] == [Segment("W ⚠ 86%", RED), Segment(" Sat 08:00", PLAIN)]


@pytest.mark.parametrize(
    ("percent", "holds", "shown"),
    [(79, [], False), (80, [], True), (10, ["model_scoped:Fable"], True)],
)
def test_model_scoped_visibility(percent: int, holds: list[str], shown: bool) -> None:
    usage = usage_doc(model_scoped=[("Fable", percent, WEEKLY_RESET)])
    rec = supervision("paused", WEEKLY_RESET, holds) if holds else None
    line = plain(ctx(stdin(45), usage=usage, record=rec))
    assert (f" ~ Fable ⚠ {percent}% Sat 08:00" in line) is shown


def test_multiple_model_scoped_in_order() -> None:
    usage = usage_doc(
        weekly=(81, WEEKLY_RESET),
        model_scoped=[("Fable", 82, WEEKLY_RESET), ("Opus", 20, None), ("Sonnet", 90, None)],
    )
    line = plain(ctx(stdin(45), usage=usage))
    assert line.endswith(" ~ W ⚠ 81% Sat 08:00 ~ Fable ⚠ 82% Sat 08:00 ~ Sonnet ⚠ 90%")


def extra(enabled: bool = True, percent: int = 32) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "percent": percent,
        "used": 3.2,
        "limit": 10.0,
        "currency": "EUR",
        "disabled_reason": None,
    }


@pytest.mark.parametrize("spill", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("extra_pct", "session_pct", "holds"),
    [
        (79, 45, []),
        (80, 45, []),
        (10, 100, []),
        (10, 45, ["extra_usage"]),
    ],
)
def test_extra_usage_matrix(
    spill: bool, enabled: bool, extra_pct: int, session_pct: int, holds: list[str]
) -> None:
    usage = usage_doc(extra_usage=extra(enabled, extra_pct))
    rec = supervision("paused", None, holds) if holds else None
    lim = Limits(spill=spill)
    line = plain(ctx(stdin(session_pct), usage=usage, record=rec, limits=lim))
    trigger = extra_pct >= 80 or session_pct >= 100 or bool(holds)
    expected = spill and enabled and trigger
    assert (" ~ € 3.20/10.00" in line) is expected


def test_extra_usage_color_and_currency() -> None:
    usage = usage_doc(extra_usage=extra(True, 85) | {"currency": "CHF", "limit": None})
    segments = render(ctx(stdin(45), usage=usage, limits=Limits(spill=True)))
    assert segments[-1] == Segment("CHF 3.20", RED)


def test_supervisor_offline_appended_yellow() -> None:
    segments = render(ctx(stdin(45), offline=True))
    assert segments[-2:] == [Segment(" ~ ", GRAY), Segment("⚠ supervisor offline", YELLOW)]


def test_offline_with_wrapper_and_no_data() -> None:
    line = plain(ctx(stdin(), wrapper_id="w1", offline=True))
    assert line == HEAD + "?% ░░░░░░░░░░ ~ ⚠ supervisor offline"


def test_unsupervised_session_never_paused() -> None:
    rec = supervision("paused", SESSION_RESET, ["session"])
    context = RenderContext(
        profile_id="work",
        name="Work",
        emoji="💼",
        limits=Limits(),
        now=NOW,
        tz=TZ,
        stdin=stdin(95),
        session_record=rec,
        wrapper_id=None,
    )
    line = plain(context)
    assert "⏸" not in line and "override" not in line
    assert "⚠ 95%" in line


def test_wrapper_without_record_shows_no_supervision() -> None:
    line = plain(ctx(stdin(45), wrapper_id="w1"))
    assert line == HEAD + "45% ▓▓▓▓▓░░░░░ 20:00 (in 2h 13m)"


def test_custom_thresholds_and_colors() -> None:
    context = RenderContext(
        profile_id="work",
        name="Work",
        emoji="💼",
        limits=Limits(session_warn=60),
        now=NOW,
        tz=TZ,
        stdin=stdin(65),
        yellow_from=30,
        red_from=90,
    )
    segments = render(context)
    assert Segment("⚠ 65% ▓▓▓▓▓▓▓░░░", YELLOW) in segments


# ---------------------------------------------------------------- helpers


def test_to_ansi_resets_each_colored_segment() -> None:
    segments = [Segment("a"), Segment(" ~ ", GRAY), Segment("45%", GREEN), Segment("!", RED)]
    assert to_ansi(segments) == "a\x1b[90m ~ \x1b[0m\x1b[32m45%\x1b[0m\x1b[31m!\x1b[0m"


def test_small_helpers() -> None:
    assert bar(None) == "░" * 10
    assert bar(4) == "░" * 10
    assert bar(5) == "▓" + "░" * 9
    assert bar(250) == "▓" * 10
    assert level_for(None) == GRAY
    assert level_for(49) == GREEN and level_for(50) == YELLOW and level_for(80) == RED
    assert display_percent(-3) == 0 and display_percent(True) is None
    assert display_percent(float("nan")) is None
    assert currency_symbol("USD") == "$" and currency_symbol("GBP") == "£"
    assert currency_symbol("CHF") == "CHF" and currency_symbol(None) == ""
    assert fallback_line("Work", "💼") == "💼 Work ~ ?%"
    assert fallback_line("Work", "") == "Work ~ ?%"


def test_limits_from_mapping_defaults_and_values() -> None:
    lim = Limits.from_mapping(
        {"session": {"warn": 70, "pause": 85}, "extra_usage": {"spill": True, "warn": 60}}
    )
    assert (lim.session_warn, lim.session_pause, lim.weekly_pause) == (70, 85, 95)
    assert lim.spill is True and lim.extra_warn == 60 and lim.extra_pause == 90
    assert Limits.from_mapping(None).session_warn == 80


def test_from_constants() -> None:
    constants = {
        "profile": {"id": "work", "name": "Work", "emoji": "💼"},
        "limits": {"session": {"warn": 80, "pause": 90}},
        "colors": {"yellow_from": 40, "red_from": 70},
    }
    context = RenderContext.from_constants(constants, now=NOW, tz=TZ, stdin=stdin(45))
    assert Segment("45% ▓▓▓▓▓░░░░░", YELLOW) in render(context)


# ---------------------------------------------------------------- shared time vectors


def _vectors() -> list[dict[str, Any]]:
    data = json.loads(TIME_VECTORS.read_text("utf-8"))
    assert isinstance(data, list)
    return data


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: str(v["name"]))
def test_time_vectors(vector: dict[str, Any]) -> None:
    now = datetime.fromisoformat(vector["now"].replace("Z", "+00:00"))
    reset = datetime.fromisoformat(vector["reset"].replace("Z", "+00:00"))
    context = RenderContext(
        profile_id="work",
        name="Work",
        emoji="💼",
        limits=Limits(),
        now=now,
        tz=ZoneInfo(vector["tz"]),
        stdin=stdin(45, session_reset=reset if reset > now else None),
    )
    line = plain(context)
    if reset > now:
        assert line.endswith(f"45% ▓▓▓▓▓░░░░░ {vector['combined']}")
    else:
        assert line.endswith("45% ▓▓▓▓▓░░░░░")
