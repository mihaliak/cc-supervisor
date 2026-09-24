"""Apply / revert the generated statusline in a profile's `settings.json` (P07).

`apply` backs up `settings.json`, records the previous `statusLine` in
`statusline/<profile_id>.json` and sets ours. `revert` restores the recorded value unless the
user changed `statusLine` since (conflict). Key order, other keys and the file mode are kept.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ccs import paths
from ccs.clock import Clock, SystemClock
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.fsio import atomic_write_json, atomic_write_text, read_json
from ccs.statusline import template

log = logging.getLogger(__name__)

SETTINGS_NAME = "settings.json"
BACKUP_PREFIX = "settings.json.ccs-backup-"
BOOKKEEPING_SCHEMA = 1

APPLIED = "applied"
ALREADY_APPLIED = "already_applied"
REVERTED = "reverted"
NOT_APPLIED = "not_applied"
CONFLICT = "conflict"


class ApplyError(Exception):
    """`apply`/`revert` refused: `code` is `statusline_disabled`, `invalid_settings`, …"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ApplyResult:
    profile_id: str
    settings_path: Path
    result: str
    backup_path: Path | None
    command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "settings_path": str(self.settings_path),
            "result": self.result,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "command": self.command,
        }


@dataclass(frozen=True)
class RevertResult:
    profile_id: str
    result: str
    restored: Any
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "profile_id": self.profile_id,
            "result": self.result,
            "restored": self.restored,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


def settings_path(profile: Profile) -> Path:
    """`<config_dir>/settings.json` (config dir as given, symlinks kept)."""
    return Path(profile.config_dir_env) / SETTINGS_NAME


def read_settings(path: Path) -> dict[str, Any] | None:
    """Parsed `settings.json` (key order kept); `None` when missing.

    Raises `ApplyError("invalid_settings")` for unreadable, invalid or non-object content.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ApplyError("invalid_settings", f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ApplyError("invalid_settings", f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApplyError("invalid_settings", f"{path} is not a JSON object")
    return data


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    """Atomic write with key order kept, indent 2, UTF-8, trailing newline, same file mode."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o644
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text, mode)


def is_ours(value: Any, script: Path) -> bool:
    """A `statusLine` pointing at this profile's generated script (any interpreter)."""
    if not isinstance(value, dict) or value.get("type") != "command":
        return False
    command = value.get("command")
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return bool(parts) and parts[-1] == str(script) and "-S" in parts and "-E" in parts


def _backup(path: Path, now: datetime) -> Path:
    stamp = now.astimezone().strftime("%Y%m%d%H%M%S")
    candidate = path.with_name(f"{BACKUP_PREFIX}{stamp}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{BACKUP_PREFIX}{stamp}-{n}")
        n += 1
    shutil.copy2(path, candidate)
    return candidate


def read_bookkeeping(profile_id: str) -> dict[str, Any] | None:
    """`statusline/<profile_id>.json`, if present."""
    return read_json(paths.statusline_file(profile_id))


def is_applied(profile: Profile) -> bool:
    """`settings.json` currently points at this profile's generated script."""
    try:
        settings = read_settings(settings_path(profile))
    except ApplyError:
        return False
    return settings is not None and is_ours(
        settings.get("statusLine"), template.script_path(profile)
    )


def apply(profile: Profile, config: Config, *, clock: Clock | None = None) -> ApplyResult:
    """Point `settings.json` `statusLine` at the generated script (idempotent, with backup)."""
    if not profile.statusline.enabled:
        raise ApplyError(
            "statusline_disabled",
            f"statusline is disabled for profile '{profile.id}' (statusline.enabled=false)",
        )
    now = (clock or SystemClock()).now()
    script = template.ensure_script(profile, config)
    command = template.statusline_command(script)
    wanted = {"type": "command", "command": command}
    path = settings_path(profile)
    settings = read_settings(path)  # validate before anything is written
    if settings is not None and settings.get("statusLine") == wanted:
        return ApplyResult(profile.id, path, ALREADY_APPLIED, None, command)
    # Re-read right before writing to shrink the race with Claude Code's own writes.
    settings = read_settings(path)
    current = settings.get("statusLine") if settings is not None else None
    if settings is not None and current == wanted:
        return ApplyResult(profile.id, path, ALREADY_APPLIED, None, command)
    previous_book = read_bookkeeping(profile.id)
    if is_ours(current, script) and previous_book is not None:
        previous = previous_book.get("previous_statusline")  # ours with another interpreter
    else:
        previous = current
    backup = _backup(path, now) if settings is not None else None
    data: dict[str, Any] = dict(settings) if settings is not None else {}
    data["statusLine"] = wanted
    atomic_write_json(
        paths.statusline_file(profile.id),
        {
            "schema": BOOKKEEPING_SCHEMA,
            "profile_id": profile.id,
            "config_dir": profile.config_dir_env,
            "settings_path": str(path),
            "command": command,
            "previous_statusline": previous,
            "applied_at": now.astimezone().isoformat(timespec="seconds"),
            "backup_path": str(backup) if backup else None,
        },
    )
    _write_settings(path, data)
    return ApplyResult(profile.id, path, APPLIED, backup, command)


def revert(profile: Profile) -> RevertResult:
    """Restore the `statusLine` recorded by `apply`; the script file is kept (launcher uses it)."""
    book = read_bookkeeping(profile.id)
    if book is None:
        return RevertResult(profile.id, NOT_APPLIED, None)
    path = settings_path(profile)
    settings = read_settings(path)
    script = template.script_path(profile)
    current = settings.get("statusLine") if settings is not None else None
    if settings is None or not is_ours(current, script):
        return RevertResult(
            profile.id,
            CONFLICT,
            None,
            hint=(
                f"{path} no longer points at the ccs statusline; edit statusLine manually "
                f"(previous value is in {paths.statusline_file(profile.id)})"
            ),
        )
    previous = book.get("previous_statusline")
    data = dict(settings)
    if previous is None:
        data.pop("statusLine", None)
    else:
        data["statusLine"] = previous
    _write_settings(path, data)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(paths.statusline_file(profile.id))
    return RevertResult(profile.id, REVERTED, previous)


def ensure_statusline(profile: Profile, config: Config | None = None) -> str | None:
    """Launcher hook (P05): make the script current and return its `statusLine` command.

    `None` when the profile's statusline is disabled or the script can't be written (the
    launcher then starts claude without injecting a statusline). Loads the config if not given.
    Never touches `settings.json`.
    """
    if not profile.statusline.enabled:
        return None
    try:
        cfg = config if config is not None else store.load()[0]
        return template.statusline_command(template.ensure_script(profile, cfg))
    except Exception:
        log.warning("statusline script for profile %s unavailable", profile.id, exc_info=True)
        return None


# The name the P05 plan uses for the launcher hook (returns the command string, not a path).
ensure_script = ensure_statusline
