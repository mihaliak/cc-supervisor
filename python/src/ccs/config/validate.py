"""Config validation (ADR-0004). Never raises; returns a list of `Issue`s."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, TypeGuard

from ccs.config import defaults
from ccs.config.models import deep_merge
from ccs.paths import expand_config_dir

# Patterns are applied with `fullmatch` (a trailing newline never passes) and use ASCII
# classes only; `schema/config.schema.json` carries the same patterns anchored with ^…$.
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
# `haiku`, `sonnet`, `opus[1m]`, `claude-opus-5-5`, provider ids (`us.anthropic.…-v1:0`, `…@2025…`)
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,127}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
PROMPT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # newline and tab allowed
# The only keys a config file must spell out; everything else takes a default (the schema's
# `required` lists the same keys).
REQUIRED_KEYS = ("version", "profiles")
POLL_INTERVAL_MIN_S = 5
POLL_INTERVAL_MAX_S = 240  # the 5-minute heartbeat rule (ADR-0005/0009, ADR-0022)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# `emoji_percent` is the pre-ADR-0018 name of `letter_percent`; still accepted.
MENU_BAR_MODES = ("letter_percent", "icon_only", "emoji_percent")
TIME_FORMATS = ("24h",)

CCS_RESERVED = frozenset({"help", "version", "profile", "force", "no-supervise", "json"})

# Every long option in `claude --help`, Claude Code 2.1.281, captured 2026-09-24.
# `ccs doctor` (P13) diffs this against the installed claude and warns when it drifts.
CLAUDE_LONG_OPTIONS = frozenset(
    {
        "add-dir",
        "agent",
        "agents",
        "all",
        "allow-dangerously-skip-permissions",
        "allowed-tools",
        "allowedTools",
        "append-system-prompt",
        "autocompact",
        "ax-screen-reader",
        "background",
        "bare",
        "betas",
        "bg",
        "brief",
        "chrome",
        "cloud",
        "continue",
        "dangerously-skip-permissions",
        "debug",
        "debug-file",
        "disable-slash-commands",
        "disallowed-tools",
        "disallowedTools",
        "effort",
        "environment",
        "exclude-dynamic-system-prompt-sections",
        "fallback-model",
        "file",
        "fork-session",
        "forward-subagent-text",
        "from-pr",
        "help",
        "ide",
        "include-hook-events",
        "include-partial-messages",
        "input-format",
        "json-schema",
        "max-budget-usd",
        "mcp-config",
        "model",
        "name",
        "no-chrome",
        "no-session-persistence",
        "output-format",
        "permission-mode",
        "permission-prompt-tool",
        "permission-prompts",
        "plugin-dir",
        "plugin-url",
        "print",
        "prompt-suggestions",
        "remote-control",
        "remote-control-session-name-prefix",
        "replay-user-messages",
        "restricted",
        "resume",
        "safe-mode",
        "session-id",
        "setting-sources",
        "settings",
        "strict-mcp-config",
        "system-prompt",
        "system-prompt-snapshot",
        "teleport",
        "tmux",
        "tools",
        "verbose",
        "version",
        "worktree",
    }
)

RESERVED_FLAGS = CCS_RESERVED | CLAUDE_LONG_OPTIONS


@dataclass(frozen=True)
class Issue:
    """One validation problem at a key path such as `profiles[1].limits.session.pause`."""

    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def _is_int(v: Any) -> TypeGuard[int]:
    return isinstance(v, int) and not isinstance(v, bool)


def is_path(v: Any) -> bool:
    """An absolute or `~`-prefixed path without control characters (ADR-0004/0022)."""
    return isinstance(v, str) and v.startswith(("/", "~")) and not CONTROL_RE.search(v)


class _V:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, path: str, message: str) -> None:
        self.issues.append(Issue(path, message))

    def dict_at(self, d: dict[str, Any], key: str, path: str) -> dict[str, Any] | None:
        v = d.get(key)
        if not isinstance(v, dict):
            self.add(path, "must be an object")
            return None
        return v

    def bool_at(self, d: dict[str, Any], key: str, path: str) -> None:
        if not isinstance(d.get(key), bool):
            self.add(path, "must be true or false")

    def int_range(self, d: dict[str, Any], key: str, path: str, lo: int, hi: int) -> int | None:
        v = d.get(key)
        if not _is_int(v) or not lo <= v <= hi:
            self.add(path, f"must be a whole number from {lo} to {hi}")
            return None
        return int(v)

    def nonempty_str(self, d: dict[str, Any], key: str, path: str) -> str | None:
        v = d.get(key)
        if not isinstance(v, str) or not v.strip():
            self.add(path, "must be a non-empty string")
            return None
        return v

    def text(self, d: dict[str, Any], key: str, path: str) -> str | None:
        """A non-empty single-line string (names, emoji): no control characters."""
        v = self.nonempty_str(d, key, path)
        if v is not None and CONTROL_RE.search(v):
            self.add(path, "must not contain control characters")
            return None
        return v

    def prompt(self, d: dict[str, Any], key: str, path: str) -> str | None:
        """A non-empty prompt: control characters other than newline and tab are rejected."""
        v = self.nonempty_str(d, key, path)
        if v is not None and PROMPT_CONTROL_RE.search(v):
            self.add(path, "must not contain control characters (newline and tab are fine)")
            return None
        return v

    def time_str(self, d: dict[str, Any], key: str, path: str) -> None:
        v = d.get(key)
        if not isinstance(v, str) or not TIME_RE.fullmatch(v):
            self.add(path, "must be a 24h time HH:MM")

    def warn_pause(self, d: dict[str, Any], path: str) -> None:
        warn = self.int_range(d, "warn", f"{path}.warn", 1, 100)
        pause = self.int_range(d, "pause", f"{path}.pause", 1, 100)
        if warn is not None and pause is not None and warn >= pause:
            self.add(f"{path}.warn", f"warn ({warn}) must be lower than pause ({pause})")


def _validate_top(v: _V, d: dict[str, Any]) -> None:
    version = d.get("version")
    if not _is_int(version):
        v.add("version", "must be 1")
    elif version > defaults.CONFIG_VERSION:
        v.add(
            "version",
            f"config version {version} is newer than this ccs supports "
            f"({defaults.CONFIG_VERSION}); upgrade ccs",
        )
    elif version != defaults.CONFIG_VERSION:
        v.add("version", "must be 1")
    rev = d.get("revision")
    if not _is_int(rev) or rev < 0:
        v.add("revision", "must be a whole number >= 0")
    for key in ("ccs_path", "claude_path"):
        val = d.get(key)
        if val is not None and not is_path(val):
            v.add(key, "must be null or an absolute path (or start with ~)")

    display = v.dict_at(d, "display", "display")
    if display is not None:
        if display.get("time_format") not in TIME_FORMATS:
            v.add("display.time_format", 'must be "24h"')
        if display.get("menu_bar") not in MENU_BAR_MODES:
            v.add("display.menu_bar", 'must be "letter_percent" or "icon_only"')
        colors = v.dict_at(display, "colors", "display.colors")
        if colors is not None:
            y = colors.get("yellow_from")
            r = colors.get("red_from")
            if not (_is_int(y) and _is_int(r) and 0 < y < r <= 100):
                v.add("display.colors", "must satisfy 0 < yellow_from < red_from <= 100")

    polling = v.dict_at(d, "polling", "polling")
    if polling is not None:
        for key in ("interval_seconds", "fast_interval_seconds", "idle_interval_seconds"):
            v.int_range(polling, key, f"polling.{key}", POLL_INTERVAL_MIN_S, POLL_INTERVAL_MAX_S)
        v.int_range(
            polling, "fast_when_percent_at_least", "polling.fast_when_percent_at_least", 1, 100
        )

    notifications = v.dict_at(d, "notifications", "notifications")
    if notifications is not None:
        for key in ("limit_warn", "limit_pause", "limit_resume", "warmup", "errors"):
            v.bool_at(notifications, key, f"notifications.{key}")


def _validate_warmup(v: _V, w: dict[str, Any], path: str) -> None:
    v.bool_at(w, "enabled", f"{path}.enabled")
    model = w.get("model")
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        v.add(f"{path}.model", "must be a model name or id such as haiku or claude-opus-5-5")
    prompt = v.prompt(w, "prompt", f"{path}.prompt")
    if prompt is not None and prompt.startswith("-"):
        v.add(f"{path}.prompt", "must not start with - (claude would read it as an option)")
    v.int_range(w, "cooldown_minutes", f"{path}.cooldown_minutes", 0, 1440)
    triggers = v.dict_at(w, "triggers", f"{path}.triggers")
    if triggers is not None:
        for key in ("app_start", "unlock_wake", "auto_chain"):
            v.bool_at(triggers, key, f"{path}.triggers.{key}")
        schedule = triggers.get("schedule")
        if not isinstance(schedule, list):
            v.add(f"{path}.triggers.schedule", "must be a list")
        else:
            for i, entry in enumerate(schedule):
                epath = f"{path}.triggers.schedule[{i}]"
                if not isinstance(entry, dict):
                    v.add(epath, "must be an object with time and weekdays")
                    continue
                v.time_str(entry, "time", f"{epath}.time")
                days = entry.get("weekdays")
                if not isinstance(days, list) or not days:
                    v.add(f"{epath}.weekdays", "must be a non-empty list of mon..sun")
                elif any(day not in WEEKDAYS for day in days) or len(set(days)) != len(days):
                    v.add(f"{epath}.weekdays", "must use mon..sun without duplicates")
    hours = v.dict_at(w, "active_hours", f"{path}.active_hours")
    if hours is not None:
        v.time_str(hours, "start", f"{path}.active_hours.start")
        v.time_str(hours, "end", f"{path}.active_hours.end")


def _validate_profile(v: _V, raw: Any, path: str) -> None:
    if not isinstance(raw, dict):
        v.add(path, "must be an object")
        return
    p = deep_merge(defaults.profile_defaults(), raw)
    for key in ("id", "flag"):
        val = p.get(key)
        if not isinstance(val, str) or not SLUG_RE.fullmatch(val):
            v.add(
                f"{path}.{key}",
                "must be 1-32 chars of a-z, 0-9 and -, starting with a letter or digit",
            )
    flag = p.get("flag")
    if isinstance(flag, str) and flag in RESERVED_FLAGS:
        v.add(f"{path}.flag", f"'{flag}' is reserved (ccs or claude option)")
    v.text(p, "name", f"{path}.name")
    emoji = v.text(p, "emoji", f"{path}.emoji")
    if emoji is not None and len(emoji) > 8:
        v.add(f"{path}.emoji", "must be at most 8 characters")
    if not is_path(p.get("config_dir")):
        v.add(
            f"{path}.config_dir",
            "must be an absolute path or start with ~ (no control characters)",
        )

    limits = v.dict_at(p, "limits", f"{path}.limits")
    if limits is not None:
        for key in ("session", "weekly", "model_scoped", "extra_usage"):
            sub = v.dict_at(limits, key, f"{path}.limits.{key}")
            if sub is not None:
                v.warn_pause(sub, f"{path}.limits.{key}")
        ms = limits.get("model_scoped")
        if isinstance(ms, dict):
            v.bool_at(ms, "warn_only", f"{path}.limits.model_scoped.warn_only")
        eu = limits.get("extra_usage")
        if isinstance(eu, dict):
            v.bool_at(eu, "spill", f"{path}.limits.extra_usage.spill")

    sup = v.dict_at(p, "supervisor", f"{path}.supervisor")
    if sup is not None:
        v.bool_at(sup, "enabled", f"{path}.supervisor.enabled")
        v.prompt(sup, "resume_prompt", f"{path}.supervisor.resume_prompt")
    sl = v.dict_at(p, "statusline", f"{path}.statusline")
    if sl is not None:
        v.bool_at(sl, "enabled", f"{path}.statusline.enabled")
    warmup = v.dict_at(p, "warmup", f"{path}.warmup")
    if warmup is not None:
        _validate_warmup(v, warmup, f"{path}.warmup")


def _check_finite(v: _V, node: Any, path: str) -> None:
    """Reject `Infinity`/`NaN` (and `1e999`) anywhere, unknown keys included.

    Python's `json` reads them, but they aren't JSON: Swift's decoder refuses the whole file.
    """
    if isinstance(node, float) and not math.isfinite(node):
        v.add(path, "must be a finite number")
    elif isinstance(node, dict):
        for key, value in node.items():
            _check_finite(v, value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _check_finite(v, value, f"{path}[{i}]")


def validate(raw: Any) -> list[Issue]:
    """Validate a raw config dict (missing optional keys take defaults). Never raises."""
    v = _V()
    if not isinstance(raw, dict):
        return [Issue("", "config must be a JSON object")]
    try:
        for key in REQUIRED_KEYS:
            if key not in raw:
                v.add(key, "is required")
        _check_finite(v, raw, "")
        d = deep_merge(defaults.default_config_dict(), raw)
        _validate_top(v, d)
        profiles = d.get("profiles")
        if not isinstance(profiles, list):
            v.add("profiles", "must be a list")
            return v.issues
        seen: dict[str, dict[Any, int]] = {"id": {}, "flag": {}, "config_dir": {}}
        for i, prof in enumerate(profiles):
            path = f"profiles[{i}]"
            _validate_profile(v, prof, path)
            if not isinstance(prof, dict):
                continue
            for key in ("id", "flag"):
                val = prof.get(key)
                if isinstance(val, str):
                    if val in seen[key]:
                        v.add(f"{path}.{key}", f"duplicate {key} '{val}'")
                    seen[key][val] = i
            cdir = prof.get("config_dir")
            if isinstance(cdir, str) and is_path(cdir):
                ident = expand_config_dir(cdir)
                if ident in seen["config_dir"]:
                    other = seen["config_dir"][ident]
                    v.add(f"{path}.config_dir", f"same config dir as profiles[{other}]")
                seen["config_dir"][ident] = i
        # Only what the file says: a missing `default_profile` is fine (the "personal" default
        # applies only if that profile exists, see `Config.from_dict`), and so is null.
        default = raw.get("default_profile")
        if default is not None and not isinstance(default, str):
            v.add("default_profile", "must be null or the id of a profile")
        elif isinstance(default, str) and profiles and default not in seen["id"]:
            v.add("default_profile", f"'{default}' is not the id of a profile")
    except Exception as exc:  # never raise from validation
        v.add("", f"could not validate: {exc}")
    return v.issues
