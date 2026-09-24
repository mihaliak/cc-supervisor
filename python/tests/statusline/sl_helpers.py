"""Shared helpers for the statusline (P07) tests (imported as `sl_helpers`)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ccs import paths
from ccs.config import defaults
from ccs.config.models import Config
from ccs.fsio import atomic_write_json

TZ = ZoneInfo("Europe/Bratislava")
# Thu 24 Sep 2026 17:47 local; a session reset at 18:00Z reads "20:00 (in 2h 13m)".
NOW = datetime(2026, 9, 24, 15, 47, tzinfo=UTC)
SESSION_RESET = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
WEEKLY_RESET = datetime(2026, 9, 26, 6, 0, tzinfo=UTC)  # "Sat 08:00"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statusline"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def make_config(config_dir: Path, **profile_overrides: Any) -> Config:
    """A config with one `work` profile (💼 Work) whose config dir is `config_dir`."""
    raw = defaults.default_config_dict()
    prof = defaults.default_profile_dict("work", "work", "Work", "💼", str(config_dir))
    for key, value in profile_overrides.items():
        prof[key] = value
    raw["profiles"] = [prof]
    raw["default_profile"] = "work"
    return Config.from_dict(raw)


def write_config(config: Config) -> Path:
    """Persist `config` as the real (XDG-isolated) config file."""
    path = paths.config_file()
    atomic_write_json(path, config.to_dict())
    return path


def usage_doc(
    *,
    status: str = "ok",
    fetched_at: datetime = NOW,
    session: tuple[int, datetime | None] | None = None,
    weekly: tuple[int, datetime | None] | None = None,
    model_scoped: list[tuple[str, int, datetime | None]] | None = None,
    extra_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A `usage/<profile>.json` document."""

    def win(value: tuple[int, datetime | None] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        pct, reset = value
        return {
            "percent": pct,
            "resets_at": iso(reset) if reset else None,
            "observed_at": iso(fetched_at),
            "source": "get_usage",
        }

    return {
        "schema": 1,
        "profile_id": "work",
        "status": status,
        "error": None,
        "fetched_at": iso(fetched_at),
        "polled_at": iso(fetched_at),
        "subscription_type": "max",
        "windows": {
            "session": win(session),
            "weekly": win(weekly),
            "model_scoped": [
                {
                    "name": n,
                    "percent": p,
                    "resets_at": iso(r) if r else None,
                    "observed_at": iso(fetched_at),
                }
                for n, p, r in (model_scoped or [])
            ],
        },
        "extra_usage": extra_usage,
    }


def new_config_dir(tmp_path: Path) -> Path:
    """An empty stand-in for a Claude config dir under `tmp_path`."""
    d = tmp_path / "claude-work"
    d.mkdir(exist_ok=True)
    return d
