"""Typed, immutable view of `config.json` (ADR-0004).

`from_dict` fills missing optional keys from `defaults.py`; every object keeps unknown
keys in `extra` so `to_dict` re-emits them (forward compatibility).
Expects a dict that passed `validate`; use `store.load()` rather than calling it on raw input.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ccs.config import defaults
from ccs.paths import config_dir_env_value, expand_config_dir


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return `base` overlaid with `override`; nested dicts merge, everything else replaces."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _extra(d: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in d.items() if k not in known}


def _emit(known: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(extra)
    out.update(known)
    return out


@dataclass(frozen=True)
class WarnPause:
    """A warn/pause threshold pair in percent."""

    warn: int
    pause: int
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("warn", "pause")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WarnPause:
        return cls(int(d["warn"]), int(d["pause"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        return _emit({"warn": self.warn, "pause": self.pause}, self.extra)


@dataclass(frozen=True)
class ModelScopedLimits:
    """Thresholds for model-scoped weekly windows (e.g. Fable)."""

    warn: int
    pause: int
    warn_only: bool
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("warn", "pause", "warn_only")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelScopedLimits:
        return cls(int(d["warn"]), int(d["pause"]), bool(d["warn_only"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        known = {"warn": self.warn, "pause": self.pause, "warn_only": self.warn_only}
        return _emit(known, self.extra)


@dataclass(frozen=True)
class ExtraUsageLimits:
    """Thresholds for extra usage (paid credits) and the spill toggle."""

    spill: bool
    warn: int
    pause: int
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("spill", "warn", "pause")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExtraUsageLimits:
        return cls(bool(d["spill"]), int(d["warn"]), int(d["pause"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        known = {"spill": self.spill, "warn": self.warn, "pause": self.pause}
        return _emit(known, self.extra)


@dataclass(frozen=True)
class Limits:
    """All per-profile limit thresholds (ADR-0008)."""

    session: WarnPause
    weekly: WarnPause
    model_scoped: ModelScopedLimits
    extra_usage: ExtraUsageLimits
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("session", "weekly", "model_scoped", "extra_usage")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Limits:
        return cls(
            WarnPause.from_dict(d["session"]),
            WarnPause.from_dict(d["weekly"]),
            ModelScopedLimits.from_dict(d["model_scoped"]),
            ExtraUsageLimits.from_dict(d["extra_usage"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "session": self.session.to_dict(),
            "weekly": self.weekly.to_dict(),
            "model_scoped": self.model_scoped.to_dict(),
            "extra_usage": self.extra_usage.to_dict(),
        }
        return _emit(known, self.extra)


@dataclass(frozen=True)
class SupervisorCfg:
    """Supervision on/off and the resume prompt."""

    enabled: bool
    resume_prompt: str
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("enabled", "resume_prompt")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SupervisorCfg:
        return cls(bool(d["enabled"]), str(d["resume_prompt"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        known = {"enabled": self.enabled, "resume_prompt": self.resume_prompt}
        return _emit(known, self.extra)


@dataclass(frozen=True)
class StatuslineCfg:
    """Statusline on/off for `ccs` sessions."""

    enabled: bool
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("enabled",)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StatuslineCfg:
        return cls(bool(d["enabled"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        return _emit({"enabled": self.enabled}, self.extra)


@dataclass(frozen=True)
class ScheduleEntry:
    """One scheduled warm-up time (`HH:MM`) and its weekdays (`mon`…`sun`)."""

    time: str
    weekdays: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("time", "weekdays")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScheduleEntry:
        return cls(str(d["time"]), tuple(str(w) for w in d["weekdays"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        return _emit({"time": self.time, "weekdays": list(self.weekdays)}, self.extra)


@dataclass(frozen=True)
class Triggers:
    """Warm-up triggers (ADR-0010)."""

    schedule: tuple[ScheduleEntry, ...]
    app_start: bool
    unlock_wake: bool
    auto_chain: bool
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("schedule", "app_start", "unlock_wake", "auto_chain")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Triggers:
        return cls(
            tuple(ScheduleEntry.from_dict(e) for e in d["schedule"]),
            bool(d["app_start"]),
            bool(d["unlock_wake"]),
            bool(d["auto_chain"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "schedule": [e.to_dict() for e in self.schedule],
            "app_start": self.app_start,
            "unlock_wake": self.unlock_wake,
            "auto_chain": self.auto_chain,
        }
        return _emit(known, self.extra)


@dataclass(frozen=True)
class ActiveHours:
    """Local time range (`HH:MM`) for auto-chain and missed-schedule catch-up."""

    start: str
    end: str
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("start", "end")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveHours:
        return cls(str(d["start"]), str(d["end"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        return _emit({"start": self.start, "end": self.end}, self.extra)


@dataclass(frozen=True)
class WarmupCfg:
    """Warm-up settings (ADR-0010)."""

    enabled: bool
    model: str
    prompt: str
    triggers: Triggers
    active_hours: ActiveHours
    cooldown_minutes: int
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("enabled", "model", "prompt", "triggers", "active_hours", "cooldown_minutes")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WarmupCfg:
        return cls(
            bool(d["enabled"]),
            str(d["model"]),
            str(d["prompt"]),
            Triggers.from_dict(d["triggers"]),
            ActiveHours.from_dict(d["active_hours"]),
            int(d["cooldown_minutes"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "enabled": self.enabled,
            "model": self.model,
            "prompt": self.prompt,
            "triggers": self.triggers.to_dict(),
            "active_hours": self.active_hours.to_dict(),
            "cooldown_minutes": self.cooldown_minutes,
        }
        return _emit(known, self.extra)


@dataclass(frozen=True)
class Profile:
    """One Claude Code identity (config dir) managed by CC Supervisor."""

    id: str
    flag: str
    name: str
    emoji: str
    config_dir: str
    limits: Limits
    supervisor: SupervisorCfg
    statusline: StatuslineCfg
    warmup: WarmupCfg
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = (
        "id",
        "flag",
        "name",
        "emoji",
        "config_dir",
        "limits",
        "supervisor",
        "statusline",
        "warmup",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Profile:
        d = deep_merge(defaults.profile_defaults(), raw)
        return cls(
            str(d["id"]),
            str(d["flag"]),
            str(d["name"]),
            str(d["emoji"]),
            str(d["config_dir"]),
            Limits.from_dict(d["limits"]),
            SupervisorCfg.from_dict(d["supervisor"]),
            StatuslineCfg.from_dict(d["statusline"]),
            WarmupCfg.from_dict(d["warmup"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "id": self.id,
            "flag": self.flag,
            "name": self.name,
            "emoji": self.emoji,
            "config_dir": self.config_dir,
            "limits": self.limits.to_dict(),
            "supervisor": self.supervisor.to_dict(),
            "statusline": self.statusline.to_dict(),
            "warmup": self.warmup.to_dict(),
        }
        return _emit(known, self.extra)

    @property
    def config_path(self) -> Path:
        """`config_dir` expanded and resolved (identity for comparisons)."""
        return expand_config_dir(self.config_dir)

    @property
    def config_dir_env(self) -> str:
        """Value for `CLAUDE_CONFIG_DIR` (`~` expanded, symlinks kept)."""
        return config_dir_env_value(self.config_dir)


@dataclass(frozen=True)
class Colors:
    """Level color boundaries in percent (ADR-0009)."""

    yellow_from: int
    red_from: int
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("yellow_from", "red_from")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Colors:
        return cls(int(d["yellow_from"]), int(d["red_from"]), _extra(d, cls.KEYS))

    def to_dict(self) -> dict[str, Any]:
        known = {"yellow_from": self.yellow_from, "red_from": self.red_from}
        return _emit(known, self.extra)

    def level(self, percent: int) -> str:
        """`green` below `yellow_from`, `red` at or above `red_from`, else `yellow`."""
        if percent >= self.red_from:
            return "red"
        if percent >= self.yellow_from:
            return "yellow"
        return "green"


@dataclass(frozen=True)
class Display:
    """Display settings."""

    time_format: str
    colors: Colors
    menu_bar: str
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("time_format", "colors", "menu_bar")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Display:
        return cls(
            str(d["time_format"]),
            Colors.from_dict(d["colors"]),
            str(d["menu_bar"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "time_format": self.time_format,
            "colors": self.colors.to_dict(),
            "menu_bar": self.menu_bar,
        }
        return _emit(known, self.extra)


@dataclass(frozen=True)
class Polling:
    """Usage poll cadence (ADR-0008)."""

    interval_seconds: int
    fast_interval_seconds: int
    fast_when_percent_at_least: int
    idle_interval_seconds: int
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = (
        "interval_seconds",
        "fast_interval_seconds",
        "fast_when_percent_at_least",
        "idle_interval_seconds",
    )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Polling:
        return cls(
            int(d["interval_seconds"]),
            int(d["fast_interval_seconds"]),
            int(d["fast_when_percent_at_least"]),
            int(d["idle_interval_seconds"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "interval_seconds": self.interval_seconds,
            "fast_interval_seconds": self.fast_interval_seconds,
            "fast_when_percent_at_least": self.fast_when_percent_at_least,
            "idle_interval_seconds": self.idle_interval_seconds,
        }
        return _emit(known, self.extra)


@dataclass(frozen=True)
class Notifications:
    """Per-type notification toggles (ADR-0015)."""

    limit_warn: bool
    limit_pause: bool
    limit_resume: bool
    warmup: bool
    errors: bool
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = ("limit_warn", "limit_pause", "limit_resume", "warmup", "errors")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Notifications:
        return cls(
            bool(d["limit_warn"]),
            bool(d["limit_pause"]),
            bool(d["limit_resume"]),
            bool(d["warmup"]),
            bool(d["errors"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "limit_warn": self.limit_warn,
            "limit_pause": self.limit_pause,
            "limit_resume": self.limit_resume,
            "warmup": self.warmup,
            "errors": self.errors,
        }
        return _emit(known, self.extra)


@dataclass(frozen=True)
class Config:
    """The whole `config.json`."""

    version: int
    revision: int
    default_profile: str | None
    ccs_path: str | None
    claude_path: str | None
    display: Display
    polling: Polling
    notifications: Notifications
    profiles: tuple[Profile, ...]
    extra: dict[str, Any] = field(default_factory=dict)

    KEYS = (
        "version",
        "revision",
        "default_profile",
        "ccs_path",
        "claude_path",
        "display",
        "polling",
        "notifications",
        "profiles",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        d = deep_merge(defaults.default_config_dict(), raw)
        default = d["default_profile"]
        if "default_profile" not in raw and not any(
            isinstance(p, dict) and p.get("id") == default for p in d["profiles"]
        ):
            default = None  # the "personal" default applies only when that profile exists
        return cls(
            int(d["version"]),
            int(d["revision"]),
            None if default is None else str(default),
            None if d["ccs_path"] is None else str(d["ccs_path"]),
            None if d["claude_path"] is None else str(d["claude_path"]),
            Display.from_dict(d["display"]),
            Polling.from_dict(d["polling"]),
            Notifications.from_dict(d["notifications"]),
            tuple(Profile.from_dict(p) for p in d["profiles"]),
            _extra(d, cls.KEYS),
        )

    def to_dict(self) -> dict[str, Any]:
        known = {
            "version": self.version,
            "revision": self.revision,
            "default_profile": self.default_profile,
            "ccs_path": self.ccs_path,
            "claude_path": self.claude_path,
            "display": self.display.to_dict(),
            "polling": self.polling.to_dict(),
            "notifications": self.notifications.to_dict(),
            "profiles": [p.to_dict() for p in self.profiles],
        }
        return _emit(known, self.extra)

    def profile(self, profile_id: str) -> Profile | None:
        """Profile by `id`."""
        return next((p for p in self.profiles if p.id == profile_id), None)

    def profile_by_flag(self, flag: str) -> Profile | None:
        """Profile by launcher `flag`."""
        return next((p for p in self.profiles if p.flag == flag), None)

    def default(self) -> Profile | None:
        """The `default_profile`, if it exists."""
        return self.profile(self.default_profile) if self.default_profile else None


def from_dict(raw: dict[str, Any]) -> Config:
    """Build a `Config` from a (validated) raw dict, filling defaults."""
    return Config.from_dict(raw)


def to_dict(cfg: Config) -> dict[str, Any]:
    """Serialize a `Config` back to a dict, including preserved unknown keys."""
    return cfg.to_dict()
