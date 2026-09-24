"""IO shell: probe → classify → normalize; snapshot and live-report file IO (ADR-0002/0005)."""

from __future__ import annotations

import re
from typing import Any

from ccs import fsio, paths
from ccs.claude_cli import (
    ClaudeNotFound,
    ProbeResult,
    auth_status,
    probe_usage,
    resolve_claude,
)
from ccs.clock import Clock, SystemClock
from ccs.config.models import Config, Profile
from ccs.usage.merge import LiveReport, parse_live_report
from ccs.usage.model import (
    STATUS_NEEDS_SIGN_IN,
    STATUS_NO_SUBSCRIPTION,
    STATUS_OK,
    STATUS_SOURCE_ERROR,
    UsageSnapshot,
)
from ccs.usage.normalize import MalformedPayload, normalize, snapshot_from_error

_AUTH_TEXT = re.compile(
    r"not logged in|log ?in required|please run /login|unauthori[sz]ed|\b401\b|invalid api key"
    r"|oauth token (has )?expired",
    re.IGNORECASE,
)


def _limits_unavailable(raw: dict[str, Any]) -> bool:
    return raw.get("rate_limits_available") is False or raw.get("rate_limits") is None


def needs_auth_check(result: ProbeResult) -> bool:
    """Whether `classify` needs `claude auth status` to decide (logged out vs no subscription)."""
    if result.raw is not None:
        return _limits_unavailable(result.raw)
    return bool(_AUTH_TEXT.search(f"{result.error or ''}\n{result.stderr}"))


def classify(result: ProbeResult, auth: dict[str, Any] | None) -> tuple[str, str | None]:
    """Map a probe result (plus optional auth status) to `(status, error)` (P00-S2/S4 shapes).

    A logged-out profile does not error: `get_usage` succeeds with `rate_limits_available:
    false` / `rate_limits: null`, the same as API-key or no-subscription accounts, so the
    auth status (`loggedIn`) decides between `needs_sign_in` and `no_subscription`.
    """
    if result.raw is not None:
        if _limits_unavailable(result.raw):
            if auth is not None and auth.get("loggedIn") is False:
                return STATUS_NEEDS_SIGN_IN, "not signed in to Claude Code for this profile"
            if auth is not None:
                return (
                    STATUS_NO_SUBSCRIPTION,
                    "plan rate limits are not available (API key or no Claude subscription)",
                )
            return STATUS_SOURCE_ERROR, "rate limits unavailable and auth status unknown"
        return STATUS_OK, None
    text = f"{result.error or ''}\n{result.stderr}"
    if _AUTH_TEXT.search(text) and (auth is None or auth.get("loggedIn") is not True):
        return STATUS_NEEDS_SIGN_IN, "not signed in to Claude Code for this profile"
    return STATUS_SOURCE_ERROR, result.error or "get_usage failed"


async def fetch_snapshot(
    cfg: Config | None,
    profile: Profile,
    *,
    previous: UsageSnapshot | None = None,
    clock: Clock | None = None,
    claude: str | None = None,
    timeout: float = 15.0,
) -> UsageSnapshot:
    """One poll: probe → classify → normalize (or keep `previous` windows on failure)."""
    clock = clock or SystemClock()
    try:
        exe = claude or resolve_claude(cfg)
    except ClaudeNotFound as exc:
        return snapshot_from_error(profile.id, STATUS_SOURCE_ERROR, str(exc), previous, clock.now())
    result = await probe_usage(exe, profile, timeout=timeout)
    now = clock.now()
    auth = await auth_status(exe, profile) if needs_auth_check(result) else None
    status, error = classify(result, auth)
    if status == STATUS_OK and result.raw is not None:
        try:
            return normalize(result.raw, profile_id=profile.id, fetched_at=now)
        except MalformedPayload as exc:
            status, error = STATUS_SOURCE_ERROR, f"unexpected get_usage payload: {exc}"
    return snapshot_from_error(profile.id, status, error, previous, now)


def write_snapshot(snapshot: UsageSnapshot) -> None:
    """Atomically write `usage/<profile_id>.json` (the daemon is the single writer, ADR-0006)."""
    fsio.atomic_write_json(paths.usage_file(snapshot.profile_id), snapshot.to_dict())


def read_snapshot(profile_id: str) -> UsageSnapshot | None:
    """Tolerant read of `usage/<profile_id>.json` (`None` when missing or unusable)."""
    snap = UsageSnapshot.from_dict(fsio.read_json(paths.usage_file(profile_id)))
    if snap is None or snap.profile_id != profile_id:
        return None
    return snap


def read_live_reports(profile_id: str | None = None) -> list[LiveReport]:
    """All parseable `live/*.json` reports, optionally only those for `profile_id`."""
    out: list[LiveReport] = []
    try:
        entries = sorted(paths.live_dir().glob("*.json"))
    except OSError:
        return out
    for path in entries:
        report = parse_live_report(fsio.read_json(path))
        if report is None:
            continue
        if profile_id is not None and report.profile_id not in (None, profile_id):
            continue
        out.append(report)
    return out
