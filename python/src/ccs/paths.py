"""XDG config/state locations and the state-dir layout (ADR-0005, ADR-0016)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TypeGuard

APP_DIR = "ccs"
# Ids that become file names (`wrapper_id`, `session_id`): no separators, no dots (ADR-0022).
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_valid_id(value: object) -> TypeGuard[str]:
    """True for a string id that is safe as a file name: `[A-Za-z0-9_-]{1,64}`, whole string."""
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def _checked_id(value: str, what: str) -> str:
    if not is_valid_id(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def _xdg_base(var: str, fallback: str) -> Path:
    """Return `$var` when it is set to an absolute path, else the `~`-based fallback."""
    value = os.environ.get(var, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path(os.path.expanduser(fallback))


def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/ccs` or `~/.config/ccs`."""
    return _xdg_base("XDG_CONFIG_HOME", "~/.config") / APP_DIR


def config_file() -> Path:
    """The single config file, `config_dir()/config.json`."""
    return config_dir() / "config.json"


def state_dir() -> Path:
    """`$CCS_STATE_DIR`, else `$XDG_STATE_HOME/ccs`, else `~/.local/state/ccs`."""
    override = os.environ.get("CCS_STATE_DIR", "")
    if override and os.path.isabs(override):
        return Path(override)
    return _xdg_base("XDG_STATE_HOME", "~/.local/state") / APP_DIR


def daemon_sock() -> Path:
    """Unix socket of the daemon."""
    return state_dir() / "daemon.sock"


def daemon_lock() -> Path:
    """Single-instance lock file of the daemon."""
    return state_dir() / "daemon.lock"


def log_dir() -> Path:
    """Directory for rotating logs."""
    return state_dir() / "logs"


def daemon_log() -> Path:
    """The daemon's main log file."""
    return log_dir() / "daemon.log"


def usage_dir() -> Path:
    """Directory holding one UsageSnapshot per profile."""
    return state_dir() / "usage"


def usage_file(profile_id: str) -> Path:
    """`usage/<profile_id>.json`."""
    return usage_dir() / f"{profile_id}.json"


def live_dir() -> Path:
    """Directory of statusline live reports."""
    return state_dir() / "live"


def live_file(wrapper_id: str | None = None, session_id: str | None = None) -> Path:
    """`live/<wrapper_id>.json`, or `live/session-<session_id>.json` without a wrapper.

    Raises `ValueError` for an id that is not `is_valid_id`.
    """
    if wrapper_id:
        return live_dir() / f"{_checked_id(wrapper_id, 'wrapper_id')}.json"
    if session_id:
        return live_dir() / f"session-{_checked_id(session_id, 'session_id')}.json"
    raise ValueError("live_file needs a wrapper_id or a session_id")


def sessions_dir() -> Path:
    """Directory of supervised session records."""
    return state_dir() / "sessions"


def session_file(wrapper_id: str) -> Path:
    """`sessions/<wrapper_id>.json`. Raises `ValueError` for an id that is not `is_valid_id`."""
    return sessions_dir() / f"{_checked_id(wrapper_id, 'wrapper_id')}.json"


def supervisor_dir() -> Path:
    """Directory of per-profile supervisor state (holds, ledger)."""
    return state_dir() / "supervisor"


def supervisor_file(profile_id: str) -> Path:
    """`supervisor/<profile_id>.json`."""
    return supervisor_dir() / f"{profile_id}.json"


def warmup_dir() -> Path:
    """Directory of per-profile warm-up state."""
    return state_dir() / "warmup"


def warmup_file(profile_id: str) -> Path:
    """`warmup/<profile_id>.json`."""
    return warmup_dir() / f"{profile_id}.json"


def warmup_cwd() -> Path:
    """Empty working directory used for warm-up runs."""
    return warmup_dir() / "cwd"


def statusline_dir() -> Path:
    """Directory of statusline apply bookkeeping."""
    return state_dir() / "statusline"


def statusline_file(profile_id: str) -> Path:
    """`statusline/<profile_id>.json` (previous statusLine value, backup path)."""
    return statusline_dir() / f"{profile_id}.json"


def widget_dir() -> Path:
    """Directory readable by the sandboxed widget (ADR-0012)."""
    return state_dir() / "widget"


def widget_snapshot() -> Path:
    """`widget/snapshot.json`, the display model for widgets and the menu bar."""
    return widget_dir() / "snapshot.json"


def events_file() -> Path:
    """Append-only event log."""
    return state_dir() / "events.jsonl"


def events_seen_file() -> Path:
    """Notification dedupe memory."""
    return state_dir() / "events.seen.json"


def ensure_state_layout() -> Path:
    """Create the state dir tree (root mode 0700) and return the root."""
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for sub in (
        log_dir(),
        usage_dir(),
        live_dir(),
        sessions_dir(),
        supervisor_dir(),
        warmup_dir(),
        warmup_cwd(),
        statusline_dir(),
        widget_dir(),
    ):
        sub.mkdir(parents=True, exist_ok=True)
    return root


def expand_config_dir(value: str) -> Path:
    """Expand `~` and resolve symlinks: the identity used to compare profile config dirs."""
    return Path(os.path.expanduser(value)).resolve()


def config_dir_env_value(value: str) -> str:
    """Absolute path to put into `CLAUDE_CONFIG_DIR`: `~` expanded, symlinks kept.

    Claude Code derives the Keychain service name from the path it is given
    (ADR-0003), so symlinks must not be resolved here.
    """
    return os.path.abspath(os.path.expanduser(value))


DEFAULT_CLAUDE_DIR = "~/.claude"


def is_default_claude_dir(value: str) -> bool:
    """True when `value` is Claude Code's default config dir (`~/.claude`)."""
    try:
        return expand_config_dir(value) == expand_config_dir(DEFAULT_CLAUDE_DIR)
    except OSError:
        return False


def apply_claude_config_dir(env: dict[str, str], value: str) -> dict[str, str]:
    """Point `env` at the config dir `value`: set `CLAUDE_CONFIG_DIR`, or unset it for `~/.claude`.

    Claude Code keeps its global state in `~/.claude.json` only while `CLAUDE_CONFIG_DIR` is
    unset. An explicit `CLAUDE_CONFIG_DIR=~/.claude` switches to `~/.claude/.claude.json`
    (different trust, MCP servers and onboarding state), so the default dir is never set.
    """
    if is_default_claude_dir(value):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = config_dir_env_value(value)
    return env
