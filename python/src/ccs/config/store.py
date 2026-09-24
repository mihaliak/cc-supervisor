"""Revision-safe config IO (ADR-0004): flock, optimistic `revision`, validate before write."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ccs import fsio, paths
from ccs.config.models import Config
from ccs.config.seed import seed_config
from ccs.config.validate import Issue, validate


class ConfigError(Exception):
    """Base class for config store errors."""


class ConfigMissing(ConfigError):
    """The config file does not exist."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"config file not found: {path}")
        self.path = path


class ConfigInvalid(ConfigError):
    """The config file (or a proposed change) fails validation."""

    def __init__(self, issues: list[Issue]) -> None:
        detail = "; ".join(f"{i.path or '<root>'}: {i.message}" for i in issues[:5])
        super().__init__(f"invalid config: {detail}")
        self.issues = issues


class RevisionConflict(ConfigError):
    """The file changed since the caller read it (`expected_revision` is stale)."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"config revision conflict: expected {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else paths.config_file()


def lock_path(path: Path | None = None) -> Path:
    """The lock file guarding writes to the config."""
    return _path(path).parent / ".config.lock"


def load_raw(path: Path | None = None) -> dict[str, Any]:
    """Read the raw JSON object. Raises `ConfigMissing` or `ConfigInvalid` (bad JSON)."""
    p = _path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigMissing(p) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigInvalid([Issue("", f"invalid JSON: {exc}")]) from exc
    if not isinstance(data, dict):
        raise ConfigInvalid([Issue("", "config must be a JSON object")])
    return data


def load(path: Path | None = None) -> tuple[Config, dict[str, Any]]:
    """Load, validate and parse. Returns `(config, raw)`."""
    raw = load_raw(path)
    issues = validate(raw)
    if issues:
        raise ConfigInvalid(issues)
    return Config.from_dict(raw), raw


def save(
    mutator: Callable[[dict[str, Any]], None],
    *,
    expected_revision: int | None = None,
    path: Path | None = None,
) -> Config:
    """Apply `mutator` to the raw dict under the config lock and write it atomically.

    Unknown keys survive because the mutator edits the raw dict. `revision` is bumped.
    Raises `ConfigMissing`, `RevisionConflict`, or `ConfigInvalid` (nothing is written).
    """
    p = _path(path)
    with fsio.file_lock(lock_path(p)):
        raw = load_raw(p)
        current = raw.get("revision", 0)
        current_rev = current if isinstance(current, int) and not isinstance(current, bool) else 0
        if expected_revision is not None and expected_revision != current_rev:
            raise RevisionConflict(expected_revision, current_rev)
        updated = copy.deepcopy(raw)
        mutator(updated)
        updated["revision"] = current_rev + 1
        issues = validate(updated)
        if issues:
            raise ConfigInvalid(issues)
        fsio.atomic_write_json(p, updated)
        return Config.from_dict(updated)


def create(raw: dict[str, Any], path: Path | None = None) -> Config:
    """Write a brand-new config if none exists (validated). Returns the loaded config."""
    p = _path(path)
    with fsio.file_lock(lock_path(p)):
        if not p.exists():
            issues = validate(raw)
            if issues:
                raise ConfigInvalid(issues)
            fsio.atomic_write_json(p, raw)
    return load(p)[0]


def ensure_config(path: Path | None = None) -> Config:
    """Load the config, seeding it (`seed.py`) when the file is missing."""
    try:
        return load(path)[0]
    except ConfigMissing:
        return create(seed_config(), path)
