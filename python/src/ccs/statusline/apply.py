"""Apply / revert the generated statusline in a profile's `settings.json` (P07).

`apply` backs up `settings.json`, sets ours, then records the previous `statusLine` in
`statusline/<profile_id>.json`. `revert` backs it up too and restores the recorded value in the
`settings.json` it was applied to, unless the user changed `statusLine` since (conflict). Key
order, other keys, the user's extra `statusLine` keys (e.g. `padding`) and the file mode are
kept.

Claude Code rewrites `settings.json` itself and shares no lock with us, so every write is
optimistic: remember what was read, and right before `os.replace` check the file still holds
it; otherwise re-read and redo the change (`SETTINGS_ATTEMPTS` times, then `settings_busy`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ccs import paths
from ccs.clock import Clock, SystemClock
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.fsio import atomic_write_json, read_json
from ccs.statusline import template

log = logging.getLogger(__name__)

SETTINGS_NAME = "settings.json"
BACKUP_PREFIX = "settings.json.ccs-backup-"
BOOKKEEPING_SCHEMA = 1
SETTINGS_ATTEMPTS = 5
NEW_SETTINGS_MODE = 0o644

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
    backup_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "profile_id": self.profile_id,
            "result": self.result,
            "restored": self.restored,
            "backup_path": str(self.backup_path) if self.backup_path else None,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass(frozen=True)
class _Seen:
    """What one read of `settings.json` found, to tell later whether anyone wrote it since."""

    raw: bytes | None  # the file's bytes; None when it was missing
    mode: int = NEW_SETTINGS_MODE
    stat_key: tuple[int, int, int, int] | None = None  # dev, inode, mtime_ns, size

    @property
    def exists(self) -> bool:
        return self.raw is not None


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def settings_path(profile: Profile) -> Path:
    """`<config_dir>/settings.json` (config dir as given, symlinks kept)."""
    return Path(profile.config_dir_env) / SETTINGS_NAME


def _load_settings(path: Path) -> tuple[dict[str, Any] | None, _Seen]:
    """`read_settings` plus what was read (bytes, mode and identity from the same open file)."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            raw = fh.read()
    except FileNotFoundError:
        return None, _Seen(None)
    except OSError as exc:
        raise ApplyError("invalid_settings", f"cannot read {path}: {exc}") from exc
    seen = _Seen(raw, stat.S_IMODE(st.st_mode), _stat_key(st))
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ApplyError("invalid_settings", f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise ApplyError("invalid_settings", f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApplyError("invalid_settings", f"{path} is not a JSON object")
    return data, seen


def read_settings(path: Path) -> dict[str, Any] | None:
    """Parsed `settings.json` (key order kept); `None` when missing.

    Raises `ApplyError("invalid_settings")` for unreadable, invalid or non-object content.
    """
    return _load_settings(path)[0]


def _settings_text(path: Path, data: dict[str, Any]) -> str:
    """`data` as `settings.json` text (key order kept, indent 2, trailing newline).

    Raises `ApplyError("invalid_settings")` when it can't be written back as UTF-8 (a lone
    `\\ud83d`-style surrogate escape in the file).
    """
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ApplyError(
            "invalid_settings", f"{path} holds text that can't be written back as UTF-8: {exc}"
        ) from exc
    return text


def _unchanged(path: Path, seen: _Seen) -> bool:
    """`path` still holds what `seen` read: the same file, or at least the same bytes."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return not seen.exists
    except OSError:
        return False
    if not seen.exists:
        return False
    if seen.stat_key is not None and _stat_key(st) == seen.stat_key:
        return True
    try:
        return Path(path).read_bytes() == seen.raw
    except OSError:
        return False


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace_if_unchanged(path: Path, payload: bytes, seen: _Seen) -> bool:
    """Atomically replace `path` with `payload` unless it changed since `seen` (then `False`).

    The temp file is written and synced first, so only one `stat` separates the final check
    from `os.replace`. A symlinked `settings.json` stays a symlink (its target is replaced).
    """
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, seen.mode)
        if not _unchanged(target, seen):
            tmp.unlink()
            return False
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(target.parent)
    return True


def _busy(path: Path) -> ApplyError:
    return ApplyError(
        "settings_busy",
        f"{path} kept changing while ccs was updating it (Claude Code writes it too); "
        "nothing was changed, try again",
    )


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


def _backup(path: Path, now: datetime, seen: _Seen) -> Path:
    """Save the bytes `seen` read (exactly what is about to be replaced), with the same mode."""
    stamp = now.astimezone().strftime("%Y%m%d%H%M%S")
    candidate = path.with_name(f"{BACKUP_PREFIX}{stamp}")
    n = 1
    while True:
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, seen.mode)
            break
        except FileExistsError:
            candidate = path.with_name(f"{BACKUP_PREFIX}{stamp}-{n}")
            n += 1
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(seen.raw or b"")
        os.chmod(candidate, seen.mode)
    except BaseException:
        _drop(candidate)
        raise
    return candidate


def _drop(backup: Path | None) -> None:
    """Remove a backup whose write didn't happen."""
    if backup is not None:
        with contextlib.suppress(OSError):
            backup.unlink()


def read_bookkeeping(profile_id: str) -> dict[str, Any] | None:
    """`statusline/<profile_id>.json`, if present."""
    return read_json(paths.statusline_file(profile_id))


def recorded_settings_path(book: dict[str, Any], default: Path) -> Path:
    """The `settings.json` a bookkeeping record was applied to (`default` if it has none)."""
    value = book.get("settings_path")
    return Path(value) if isinstance(value, str) and value else default


def _write_bookkeeping(
    profile: Profile,
    path: Path,
    command: str,
    previous: Any,
    now: datetime,
    backup: Path | None,
) -> None:
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


def _is_command(value: Any, command: str) -> bool:
    """`value` runs exactly `command` (extra keys such as `padding` don't matter)."""
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and value.get("command") == command
    )


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
    path = settings_path(profile)
    read_settings(path)  # validate before anything is written
    book = read_bookkeeping(profile.id)
    book_file = paths.statusline_file(profile.id)
    recorded = recorded_settings_path(book, path) if book is not None else path
    conflict: str | None = None
    if book is not None and recorded != path:
        # Applied under another config dir before: put that statusLine back first, or its
        # original value would be lost when this apply overwrites the bookkeeping.
        try:
            reverted = _revert_recorded(profile.id, book, recorded, now)
        except ApplyError as exc:
            raise ApplyError(
                exc.code,
                f"{exc}; the statusline applied there must be reverted first "
                f"(its previous value is in {book_file})",
            ) from exc
        if reverted.result == REVERTED:
            book = None
        else:
            conflict = reverted.hint
    old_script = recorded.with_name(template.SCRIPT_NAME)
    for _ in range(SETTINGS_ATTEMPTS):
        settings, seen = _load_settings(path)
        current = settings.get("statusLine") if settings is not None else None
        if conflict is not None and not is_ours(current, old_script):
            # Only a moved dir (its settings still run the old script) carries the old record
            # over. Anything else would overwrite a record that was never restored; the
            # uninstaller keeps such records too, so refuse instead of losing it.
            raise ApplyError(
                "conflict",
                f"{conflict}. Applying here would drop that record: restore the old statusLine "
                f"by hand if you want it back, then delete {book_file} and apply again",
            )
        if _is_command(current, command):
            if book is None or recorded != path:  # no record for this file: nothing to restore
                _write_bookkeeping(profile, path, command, None, now, None)
            return ApplyResult(profile.id, path, ALREADY_APPLIED, None, command)
        # Ours with another interpreter, or the old dir's statusLine (the dir was moved): keep
        # what it replaced. Ours without bookkeeping: nothing known, so revert removes it.
        if is_ours(current, script) or (book is not None and is_ours(current, old_script)):
            previous = book.get("previous_statusline") if book is not None else None
        else:
            previous = current
        data: dict[str, Any] = dict(settings) if settings is not None else {}
        extra = current if isinstance(current, dict) else {}
        data["statusLine"] = {**extra, "type": "command", "command": command}
        payload = _settings_text(path, data).encode("utf-8")
        backup = _backup(path, now, seen) if seen.exists else None
        try:
            written = _replace_if_unchanged(path, payload, seen)
        except BaseException:
            _drop(backup)
            raise
        if not written:  # Claude Code wrote it meanwhile: redo the change on its version
            _drop(backup)
            continue
        # Settings first: the bookkeeping must never claim "applied" while settings are unchanged.
        try:
            _write_bookkeeping(profile, path, command, previous, now, backup)
        except BaseException:
            _rollback(path, seen, payload)
            raise
        return ApplyResult(profile.id, path, APPLIED, backup, command)
    raise _busy(path)


def _rollback(path: Path, before: _Seen, written: bytes) -> None:
    """Put back what `apply` replaced, unless someone wrote it since (best effort; the backup
    remains)."""
    ours = _Seen(written, before.mode)
    try:
        if before.raw is None:
            if _unchanged(path, ours):
                path.unlink()
        elif not _replace_if_unchanged(path, before.raw, ours):
            log.warning("%s changed after apply; not rolled back", path)
    except OSError:
        log.warning("could not roll back %s", path, exc_info=True)


def _revert_recorded(
    profile_id: str, book: dict[str, Any], path: Path, now: datetime
) -> RevertResult:
    """Back up `path`, restore `book`'s previous statusLine in it and drop the bookkeeping.

    A conflict (the user changed `statusLine` since) changes nothing and keeps the bookkeeping.
    """
    script = path.with_name(template.SCRIPT_NAME)
    for _ in range(SETTINGS_ATTEMPTS):
        settings, seen = _load_settings(path)
        current = settings.get("statusLine") if settings is not None else None
        if settings is None or not is_ours(current, script):
            return RevertResult(
                profile_id,
                CONFLICT,
                None,
                hint=(
                    f"{path} no longer points at the ccs statusline; edit statusLine manually "
                    f"(previous value is in {paths.statusline_file(profile_id)})"
                ),
            )
        previous = book.get("previous_statusline")
        data = dict(settings)
        if previous is None:
            data.pop("statusLine", None)
        else:
            data["statusLine"] = previous
        payload = _settings_text(path, data).encode("utf-8")
        backup = _backup(path, now, seen)
        try:
            written = _replace_if_unchanged(path, payload, seen)
        except BaseException:
            _drop(backup)
            raise
        if not written:
            _drop(backup)
            continue
        with contextlib.suppress(FileNotFoundError):
            os.unlink(paths.statusline_file(profile_id))
        return RevertResult(profile_id, REVERTED, previous, backup_path=backup)
    raise _busy(path)


def revert(profile: Profile, *, clock: Clock | None = None) -> RevertResult:
    """Restore the `statusLine` recorded by `apply` (after a backup); the script file is kept
    (the launcher uses it).

    Works on the `settings.json` recorded at apply time, even if the profile's config dir
    changed since.
    """
    book = read_bookkeeping(profile.id)
    if book is None:
        return RevertResult(profile.id, NOT_APPLIED, None)
    now = (clock or SystemClock()).now()
    path = recorded_settings_path(book, settings_path(profile))
    return _revert_recorded(profile.id, book, path, now)


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
