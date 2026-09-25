"""Statusline IO shell: stdin + state files → one printed line (+ live report).

Embedded verbatim into the generated `ccs-statusline.py` (P07). It may import only the stdlib
and `ccs.clock`, `ccs.timefmt`, `ccs.statusline.render`. It never exits non-zero and never
prints a traceback: any failure prints `render.fallback_line(...)`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from typing import IO, Any

from ccs.clock import local_tz
from ccs.statusline.render import RenderContext, fallback_line, parse_time, render, to_ansi

OFFLINE_AFTER_S = 300  # usage file older than 5 min → supervisor offline (ADR-0009)
LIVE_REFRESH_S = 60  # rewrite an unchanged live report after 60 s (fresh `observed_at`)
LIVE_SCHEMA = 1
# Ids that become file names under the state dir: exactly `ccs.paths.ID_RE` (a test keeps them
# equal). `ccs.paths` isn't embedded: its pathlib import would cost ~6 ms per refresh.
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def safe_id(value: Any) -> str | None:
    """`value` when it is usable as a file name (`paths.is_valid_id`), else `None`."""
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_dir_for(constants: dict[str, Any], env: dict[str, str]) -> str:
    """`$CCS_STATE_DIR` when set to an absolute path, else the baked `state_dir`."""
    override = env.get("CCS_STATE_DIR", "")
    if override and os.path.isabs(override):
        return override
    return str(constants.get("state_dir") or "")


def is_offline(usage_path: str, now: datetime) -> bool:
    """Missing usage file, or its mtime older than 5 min → the daemon isn't polling."""
    try:
        mtime = os.stat(usage_path).st_mtime
    except OSError:
        return True
    return now.timestamp() - mtime > OFFLINE_AFTER_S


def live_rate_limits(stdin: dict[str, Any]) -> dict[str, Any]:
    """`{five_hour?: {percent, resets_at}, seven_day?: …}` from stdin (percent clamped 0..100)."""
    out: dict[str, Any] = {}
    raw = _dict(stdin.get("rate_limits"))
    for key in ("five_hour", "seven_day"):
        win = _dict(raw.get(key))
        pct = win.get("used_percentage", win.get("percent"))
        if isinstance(pct, bool) or not isinstance(pct, int | float) or pct != pct:
            continue
        reset = parse_time(win.get("resets_at"))
        out[key] = {
            "percent": min(max(float(pct), 0.0), 100.0),
            "resets_at": _iso(reset) if reset is not None else None,
        }
    return out


def build_live_report(
    constants: dict[str, Any],
    stdin: dict[str, Any],
    wrapper_id: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """The live report for this invocation, or `None` when stdin has no `rate_limits`."""
    rate_limits = live_rate_limits(stdin)
    if not rate_limits:
        return None
    workspace = _dict(stdin.get("workspace"))
    return {
        "schema": LIVE_SCHEMA,
        "profile_id": str(_dict(constants.get("profile")).get("id") or ""),
        "wrapper_id": wrapper_id,
        "session_id": _str(stdin.get("session_id")),
        "model_id": _str(_dict(stdin.get("model")).get("id")),
        "effort": _str(_dict(stdin.get("effort")).get("level")),
        "cwd": _str(workspace.get("current_dir")) or _str(stdin.get("cwd")),
        "observed_at": _iso(now),
        "rate_limits": rate_limits,
    }


def live_report_path(state_dir: str, report: dict[str, Any]) -> str | None:
    """`live/<wrapper_id>.json`, or `live/session-<session_id>.json` without a wrapper.

    `None` (no report) when the id that names the file isn't a safe file name.
    """
    if report.get("wrapper_id"):
        wrapper_id = safe_id(report.get("wrapper_id"))
        return os.path.join(state_dir, "live", f"{wrapper_id}.json") if wrapper_id else None
    session_id = safe_id(report.get("session_id"))
    if session_id:
        return os.path.join(state_dir, "live", f"session-{session_id}.json")
    return None


def needs_write(existing: dict[str, Any] | None, report: dict[str, Any], now: datetime) -> bool:
    """False only when the same data was written less than 60 s ago."""
    if existing is None:
        return True
    for key in ("rate_limits", "model_id", "effort"):
        if existing.get(key) != report.get(key):
            return True
    observed = parse_time(existing.get("observed_at"))
    return observed is None or (now - observed).total_seconds() >= LIVE_REFRESH_S


def write_live_report(state_dir: str, report: dict[str, Any], now: datetime) -> bool:
    """Write if changed or ≥ 60 s old (atomic replace, no fsync); True when written."""
    path = live_report_path(state_dir, report)
    if path is None or not state_dir:
        return False
    if not needs_write(_read_json(path), report, now):
        return False
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    data = json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:  # noqa: SIM105 (contextlib would cost startup time)
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def main(
    constants: dict[str, Any],
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """Read stdin and state, write the live report, print the statusline. Always returns 0."""
    out = stdout if stdout is not None else sys.stdout
    profile = _dict(constants.get("profile"))
    name = str(profile.get("name") or "")
    emoji = str(profile.get("emoji") or "")
    try:
        environ = dict(os.environ) if env is None else env
        raw = (stdin if stdin is not None else sys.stdin).read()
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {}
        data = parsed if isinstance(parsed, dict) else {}
        current = now if now is not None else datetime.now(UTC)
        state_dir = state_dir_for(constants, environ)
        profile_id = str(profile.get("id") or "")
        wrapper_id = _str(environ.get("CCS_WRAPPER_ID"))
        usage_path = os.path.join(state_dir, "usage", f"{profile_id}.json")
        usage = _read_json(usage_path) if state_dir else None
        record = (
            _read_json(os.path.join(state_dir, "sessions", f"{wrapper_id}.json"))
            if state_dir and safe_id(wrapper_id)
            else None
        )
        report = build_live_report(constants, data, wrapper_id, current)
        if report is not None:
            try:  # noqa: SIM105
                write_live_report(state_dir, report, current)
            except OSError:
                pass  # reporting is best effort; never break the statusline
        ctx = RenderContext.from_constants(
            constants,
            now=current,
            tz=local_tz(),
            stdin=data,
            usage=usage,
            session_record=record,
            wrapper_id=wrapper_id,
            supervisor_offline=is_offline(usage_path, current) if state_dir else True,
        )
        out.write(to_ansi(render(ctx)) + "\n")
    except Exception:
        try:  # noqa: SIM105
            out.write(fallback_line(name, emoji) + "\n")
        except Exception:
            pass
    try:  # noqa: SIM105
        out.flush()
    except Exception:
        pass
    return 0
