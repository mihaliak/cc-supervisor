"""Statusline rendering per ADR-0009 (pure).

This module is embedded verbatim into the generated `ccs-statusline.py` (P07), so it may
import only the stdlib and `ccs.timefmt`. It avoids `dataclasses`/`typing` at runtime to keep
the script's startup inside the 60 ms budget.

Inputs are plain dicts: Claude Code's statusline stdin, the `usage/<profile>.json` file and
the `sessions/<wrapper_id>.json` record. Nothing here reads files or the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from ccs.timefmt import format_reset_absolute, format_reset_combined

GREEN = "green"
YELLOW = "yellow"
RED = "red"
GRAY = "gray"
PLAIN = "plain"
COLORS = (GREEN, YELLOW, RED, GRAY, PLAIN)
ANSI_CODES = {GREEN: "32", YELLOW: "33", RED: "31", GRAY: "90"}
ANSI_RESET = "\x1b[0m"

SEPARATOR = " ~ "
BAR_CELLS = 10
BAR_FILLED = "▓"
BAR_EMPTY = "░"
WARN_MARK = "⚠"
PAUSE_MARK = "⏸"
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}

USAGE_FRESH_FOR = timedelta(minutes=10)

# Times beyond these can't be shifted into a local zone or rounded without `OverflowError`:
# garbage, never a real reset.
TIME_MIN = datetime.min.replace(tzinfo=UTC) + timedelta(days=2)
TIME_MAX = datetime.max.replace(tzinfo=UTC) - timedelta(days=2)

# C0 and C1 control characters (ESC, BEL, CR, LF, CSI, …): stripped from text that comes
# from stdin or state files, so it can't inject terminal escapes or break the line.
_CONTROLS = dict.fromkeys([*range(0x20), *range(0x7F, 0xA0)])

STATE_PAUSED = "paused"
STATE_OVERRIDDEN = "overridden"


class Segment:
    """A piece of the statusline with one color (`green|yellow|red|gray|plain`)."""

    __slots__ = ("color", "text")

    def __init__(self, text: str, color: str = PLAIN) -> None:
        self.text = text
        self.color = color

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Segment):
            return NotImplemented
        return self.text == other.text and self.color == other.color

    def __hash__(self) -> int:
        return hash((self.text, self.color))

    def __repr__(self) -> str:
        return f"Segment({self.text!r}, {self.color!r})"

    def to_dict(self) -> dict[str, str]:
        """`{text, color}` (preview JSON)."""
        return {"text": self.text, "color": self.color}


class Limits:
    """The thresholds the statusline needs (ADR-0008), flattened from the profile's `limits`."""

    __slots__ = (
        "extra_pause",
        "extra_warn",
        "model_pause",
        "model_warn",
        "session_pause",
        "session_warn",
        "spill",
        "weekly_pause",
        "weekly_warn",
    )

    def __init__(
        self,
        *,
        session_warn: int = 80,
        session_pause: int = 90,
        weekly_warn: int = 80,
        weekly_pause: int = 95,
        model_warn: int = 80,
        model_pause: int = 95,
        extra_warn: int = 80,
        extra_pause: int = 90,
        spill: bool = False,
    ) -> None:
        self.session_warn = session_warn
        self.session_pause = session_pause
        self.weekly_warn = weekly_warn
        self.weekly_pause = weekly_pause
        self.model_warn = model_warn
        self.model_pause = model_pause
        self.extra_warn = extra_warn
        self.extra_pause = extra_pause
        self.spill = spill

    @classmethod
    def from_mapping(cls, limits: Any) -> Limits:
        """Build from a config-shaped `limits` dict; missing parts keep the defaults."""
        d = limits if isinstance(limits, dict) else {}

        def part(key: str) -> dict[str, Any]:
            value = d.get(key)
            return value if isinstance(value, dict) else {}

        def num(src: dict[str, Any], key: str, default: int) -> int:
            value = src.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                return default
            return int(value)

        s, w, m, e = part("session"), part("weekly"), part("model_scoped"), part("extra_usage")
        return cls(
            session_warn=num(s, "warn", 80),
            session_pause=num(s, "pause", 90),
            weekly_warn=num(w, "warn", 80),
            weekly_pause=num(w, "pause", 95),
            model_warn=num(m, "warn", 80),
            model_pause=num(m, "pause", 95),
            extra_warn=num(e, "warn", 80),
            extra_pause=num(e, "pause", 90),
            spill=e.get("spill") is True,
        )


class RenderContext:
    """Everything `render` needs. Build it with keywords (or `from_constants`)."""

    __slots__ = (
        "emoji",
        "limits",
        "name",
        "now",
        "profile_id",
        "red_from",
        "session_record",
        "stdin",
        "supervisor_offline",
        "tz",
        "usage",
        "wrapper_id",
        "yellow_from",
    )

    def __init__(
        self,
        *,
        profile_id: str,
        name: str,
        emoji: str,
        limits: Limits,
        now: datetime,
        tz: tzinfo,
        stdin: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        session_record: dict[str, Any] | None = None,
        wrapper_id: str | None = None,
        supervisor_offline: bool = False,
        yellow_from: int = 50,
        red_from: int = 80,
    ) -> None:
        self.profile_id = profile_id
        self.name = name
        self.emoji = emoji
        self.limits = limits
        self.now = now
        self.tz = tz
        self.stdin = stdin if isinstance(stdin, dict) else {}
        self.usage = usage if isinstance(usage, dict) else None
        self.session_record = session_record if isinstance(session_record, dict) else None
        self.wrapper_id = wrapper_id or None
        self.supervisor_offline = supervisor_offline
        self.yellow_from = yellow_from
        self.red_from = red_from

    @classmethod
    def from_constants(
        cls,
        constants: dict[str, Any],
        *,
        now: datetime,
        tz: tzinfo,
        stdin: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        session_record: dict[str, Any] | None = None,
        wrapper_id: str | None = None,
        supervisor_offline: bool = False,
    ) -> RenderContext:
        """From the constants baked into the generated script (see `template.constants_for`)."""
        profile = _dict(constants.get("profile"))
        colors = _dict(constants.get("colors"))
        return cls(
            profile_id=str(profile.get("id") or ""),
            name=str(profile.get("name") or ""),
            emoji=str(profile.get("emoji") or ""),
            limits=Limits.from_mapping(constants.get("limits")),
            now=now,
            tz=tz,
            stdin=stdin,
            usage=usage,
            session_record=session_record,
            wrapper_id=wrapper_id,
            supervisor_offline=supervisor_offline,
            yellow_from=_int(colors.get("yellow_from"), 50),
            red_from=_int(colors.get("red_from"), 80),
        )


class Win:
    """A usage window as displayed: integer percent (or `None`) and its reset time."""

    __slots__ = ("percent", "resets_at")

    def __init__(self, percent: int | None, resets_at: datetime | None) -> None:
        self.percent = percent
        self.resets_at = resets_at


# ---------------------------------------------------------------- small helpers


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return int(value)


def display_percent(value: Any) -> int | None:
    """Half-up integer percent (`int(x + 0.5)`), never negative; `None` for non-numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(int(float(value) + 0.5), 0)


def parse_time(value: Any) -> datetime | None:
    """Epoch seconds or ISO 8601 → aware UTC; `None` when missing, unparseable or out of range."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            dt = datetime.fromtimestamp(float(value), UTC)
        elif isinstance(value, str) and value.strip():
            dt = datetime.fromisoformat(value.strip())
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        else:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    return dt if TIME_MIN <= dt <= TIME_MAX else None


def clean(text: str) -> str:
    """`text` without C0/C1 control characters (terminal escapes, newlines)."""
    return text.translate(_CONTROLS)


def level_for(percent: int | None, yellow_from: int = 50, red_from: int = 80) -> str:
    """`green` below `yellow_from`, `yellow` below `red_from`, else `red`; `gray` for no data."""
    if percent is None:
        return GRAY
    if percent >= red_from:
        return RED
    if percent >= yellow_from:
        return YELLOW
    return GREEN


def bar(percent: int | None) -> str:
    """10 cells; `filled = int(p / 10 + 0.5)` clamped to 0..10. No data → all empty."""
    if percent is None:
        return BAR_EMPTY * BAR_CELLS
    filled = min(max(int(percent / 10 + 0.5), 0), BAR_CELLS)
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_CELLS - filled)


def currency_symbol(code: Any) -> str:
    """`€`, `$`, `£`, else the ISO code itself (empty when unknown)."""
    if not isinstance(code, str):
        return ""
    return CURRENCY_SYMBOLS.get(code) or clean(code)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _text(value: Any) -> str | None:
    """A non-empty string to display (control characters stripped), else `None`."""
    return _str(clean(value)) if isinstance(value, str) else None


def _basename(path: str) -> str:
    stripped = path.rstrip("/")
    if not stripped:
        return "/" if path.startswith("/") else ""
    return stripped.rsplit("/", 1)[-1]


# ---------------------------------------------------------------- data selection


def usage_is_fresh(usage: dict[str, Any] | None, now: datetime) -> bool:
    """The usage file counts only when `status == ok` and its newest data is ≤ 10 min old."""
    if not usage or usage.get("status") != "ok":
        return False
    windows = _dict(usage.get("windows"))
    stamps = [parse_time(usage.get("fetched_at"))]
    for key in ("session", "weekly"):
        stamps.append(parse_time(_dict(windows.get(key)).get("observed_at")))
    present = [s for s in stamps if s is not None]
    return bool(present) and now - max(present) <= USAGE_FRESH_FOR


def _stdin_window(ctx: RenderContext, key: str) -> Win | None:
    raw = _dict(_dict(ctx.stdin.get("rate_limits")).get(key))
    percent = display_percent(raw.get("used_percentage", raw.get("percent")))
    if percent is None:
        return None
    reset = parse_time(raw.get("resets_at"))
    if reset is not None and reset <= ctx.now:
        return None  # that window is over; its numbers no longer apply
    return Win(percent, reset)


def _file_window(ctx: RenderContext, key: str) -> Win | None:
    if not usage_is_fresh(ctx.usage, ctx.now):
        return None
    assert ctx.usage is not None
    raw = _dict(_dict(ctx.usage.get("windows")).get(key))
    percent = display_percent(raw.get("percent"))
    if percent is None:
        return None
    reset = parse_time(raw.get("resets_at"))
    if reset is not None and reset <= ctx.now:
        return None
    return Win(percent, reset)


def session_window(ctx: RenderContext) -> Win | None:
    """stdin `rate_limits.five_hour` wins; else `usage` `windows.session`."""
    return _stdin_window(ctx, "five_hour") or _file_window(ctx, "session")


def weekly_window(ctx: RenderContext) -> Win | None:
    """stdin `rate_limits.seven_day` wins; else `usage` `windows.weekly`."""
    return _stdin_window(ctx, "seven_day") or _file_window(ctx, "weekly")


def model_scoped_windows(ctx: RenderContext) -> list[tuple[str, Win]]:
    """`(name, window)` for every model-scoped entry of a fresh usage file."""
    if not usage_is_fresh(ctx.usage, ctx.now):
        return []
    assert ctx.usage is not None
    out: list[tuple[str, Win]] = []
    raw_list = _dict(ctx.usage.get("windows")).get("model_scoped")
    for raw in raw_list if isinstance(raw_list, list) else []:
        item = _dict(raw)
        name = _text(item.get("name"))
        percent = display_percent(item.get("percent"))
        if name is None or percent is None:
            continue
        out.append((name, Win(percent, parse_time(item.get("resets_at")))))
    return out


def supervision(ctx: RenderContext) -> dict[str, Any]:
    """The session record's `supervision` block; empty for unsupervised sessions (ADR-0009)."""
    if ctx.wrapper_id is None or ctx.session_record is None:
        return {}
    return _dict(ctx.session_record.get("supervision"))


def active_holds(ctx: RenderContext) -> set[str]:
    """Holds listed on this session's record (`session`, `weekly`, `model_scoped:<n>`, …)."""
    holds = supervision(ctx).get("holds")
    return {h for h in holds if isinstance(h, str)} if isinstance(holds, list) else set()


# ---------------------------------------------------------------- segments


def _head(ctx: RenderContext) -> list[Segment]:
    """`{emoji} {name}`, `{folder}`, `{model} / {effort}` as separate parts (empty ones dropped).

    Control characters are stripped from every part: stdin is untrusted terminal output.
    """
    parts: list[Segment] = []
    title = clean(f"{ctx.emoji} {ctx.name}" if ctx.emoji else ctx.name)
    if title.strip():
        parts.append(Segment(title.strip()))
    workspace = _dict(ctx.stdin.get("workspace"))
    path = _text(workspace.get("current_dir")) or _text(ctx.stdin.get("cwd"))
    if path is not None:
        folder = _basename(path)
        if folder:
            parts.append(Segment(folder))
    model = _text(_dict(ctx.stdin.get("model")).get("display_name"))
    if model is not None:
        effort = _text(_dict(ctx.stdin.get("effort")).get("level"))
        parts.append(Segment(f"{model} / {effort}" if effort else model))
    return parts


def _pct_text(win: Win | None) -> str:
    return "?%" if win is None or win.percent is None else f"{win.percent}%"


def session_segments(ctx: RenderContext) -> list[Segment]:
    """The session part: normal, warn, paused, paused (manual), overridden or no data."""
    win = session_window(ctx)
    percent = win.percent if win is not None else None
    color = level_for(percent, ctx.yellow_from, ctx.red_from)
    core = f"{_pct_text(win)} {bar(percent)}"
    reset = win.resets_at if win is not None else None
    reset_text = f" {format_reset_combined(reset, ctx.now, ctx.tz)}" if reset is not None else ""
    sup = supervision(ctx)
    state = sup.get("state")
    if state == STATE_PAUSED:
        resume_at = parse_time(sup.get("resume_at"))
        if resume_at is None:
            holds = active_holds(ctx)
            # no reset time: a manual hold, or (spill mode) the extra-usage credit cap (P06)
            why = "credits" if "extra_usage" in holds and "manual" not in holds else "manual"
            return [Segment(f"{PAUSE_MARK} {core}", color), Segment(f" paused ({why})")]
        when = format_reset_combined(resume_at, ctx.now, ctx.tz)
        return [Segment(f"{PAUSE_MARK} {core}", color), Segment(f" paused → resumes {when}")]
    if state == STATE_OVERRIDDEN:
        return [Segment(f"{WARN_MARK} {core}", color), Segment(f"{reset_text} · override")]
    if percent is None:
        return [Segment(core, GRAY)]
    if percent >= ctx.limits.session_warn:
        return [Segment(f"{WARN_MARK} {core}", color), Segment(f"{reset_text} · limit approaching")]
    return [Segment(core, color)] + ([Segment(reset_text)] if reset_text else [])


def _limit_segments(ctx: RenderContext, label: str, win: Win | None) -> list[Segment]:
    percent = win.percent if win is not None else None
    color = level_for(percent, ctx.yellow_from, ctx.red_from)
    out = [Segment(f"{label} {WARN_MARK} {_pct_text(win)}", color)]
    if win is not None and win.resets_at is not None:
        out.append(Segment(f" {format_reset_absolute(win.resets_at, ctx.now, ctx.tz)}"))
    return out


def weekly_segments(ctx: RenderContext) -> list[Segment]:
    """` W ⚠ 86% Sat 08:00` when weekly ≥ warn or a `weekly` hold is active."""
    win = weekly_window(ctx)
    at_warn = win is not None and win.percent is not None and win.percent >= ctx.limits.weekly_warn
    if not at_warn and "weekly" not in active_holds(ctx):
        return []
    return _limit_segments(ctx, "W", win)


def model_scoped_segments(ctx: RenderContext) -> list[list[Segment]]:
    """One part per model-scoped window at/above warn or with an active `model_scoped:<n>` hold."""
    holds = active_holds(ctx)
    parts: list[list[Segment]] = []
    for name, win in model_scoped_windows(ctx):
        at_warn = win.percent is not None and win.percent >= ctx.limits.model_warn
        if at_warn or f"model_scoped:{name}" in holds:
            parts.append(_limit_segments(ctx, name, win))
    return parts


def extra_usage_segments(ctx: RenderContext) -> list[Segment]:
    """`€ 3.20/10.00` while spill is active and extra ≥ warn, an extra hold, or session ≥ 100."""
    if not ctx.limits.spill or not usage_is_fresh(ctx.usage, ctx.now):
        return []
    assert ctx.usage is not None
    extra = _dict(ctx.usage.get("extra_usage"))
    if extra.get("enabled") is not True:
        return []
    percent = display_percent(extra.get("percent"))
    session = session_window(ctx)
    show = (
        (percent is not None and percent >= ctx.limits.extra_warn)
        or "extra_usage" in active_holds(ctx)
        or (session is not None and session.percent is not None and session.percent >= 100)
    )
    if not show:
        return []
    used = extra.get("used")
    limit = extra.get("limit")
    used_f = float(used) if isinstance(used, int | float) and not isinstance(used, bool) else 0.0
    money = f"{used_f:.2f}"
    if isinstance(limit, int | float) and not isinstance(limit, bool):
        money += f"/{float(limit):.2f}"
    symbol = currency_symbol(extra.get("currency"))
    text = f"{symbol} {money}" if symbol else money
    color = level_for(percent, ctx.yellow_from, ctx.red_from) if percent is not None else PLAIN
    return [Segment(text, color)]


def render(ctx: RenderContext) -> list[Segment]:
    """The full statusline as colored segments (ADR-0009)."""
    parts: list[list[Segment]] = [[seg] for seg in _head(ctx)]
    parts.append(session_segments(ctx))
    weekly = weekly_segments(ctx)
    if weekly:
        parts.append(weekly)
    parts.extend(model_scoped_segments(ctx))
    extra = extra_usage_segments(ctx)
    if extra:
        parts.append(extra)
    if ctx.supervisor_offline:
        parts.append([Segment(f"{WARN_MARK} supervisor offline", YELLOW)])
    out: list[Segment] = []
    for i, part in enumerate(parts):
        if i:
            out.append(Segment(SEPARATOR, GRAY))
        out.extend(part)
    return out


def to_ansi(segments: list[Segment]) -> str:
    """ANSI text: 32 green, 33 yellow, 31 red, 90 gray; reset after each colored segment."""
    out: list[str] = []
    for seg in segments:
        code = ANSI_CODES.get(seg.color)
        out.append(f"\x1b[{code}m{seg.text}{ANSI_RESET}" if code else seg.text)
    return "".join(out)


def to_plain(segments: list[Segment]) -> str:
    """The text without colors."""
    return "".join(seg.text for seg in segments)


def fallback_line(name: str, emoji: str) -> str:
    """Minimal line printed when anything goes wrong: `{emoji} {name} ~ ?%`."""
    title = f"{emoji} {name}".strip() if emoji else name
    return f"{title}{SEPARATOR}?%" if title else "?%"
