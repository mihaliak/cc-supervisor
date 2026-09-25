"""Execute one warm-up (ADR-0010): `claude -p` with Haiku, then confirm the window started.

Guarded by a per-profile `asyncio.Lock` (a second call while one runs is skipped with
`in_progress`). Outcomes become events (`warmup.*`) and `warmup/<profile>.json` entries;
notification fields are added by `EventBus.emit` (P04), never here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ccs import claude_cli, paths
from ccs.clock import Clock
from ccs.config.models import Profile
from ccs.events import Event
from ccs.usage.model import UsageSnapshot, format_iso
from ccs.usage.normalize import session_window_active
from ccs.warmup import rules
from ccs.warmup.state import FAILED, SKIPPED, SUCCEEDED, WarmupStore

log = logging.getLogger(__name__)

TIMEOUT_S = 120.0
STDERR_TAIL = 500
# The usage API can lag the warm-up request by over a minute (seen after an auto-chain), so
# the window is polled again after these delays before `window_not_started` (ADR-0022).
CONFIRM_DELAYS_S = (10.0, 20.0, 30.0, 60.0, 60.0)

# Failure reasons (besides `exit_<code>`).
TIMEOUT = "timeout"
WINDOW_NOT_STARTED = "window_not_started"
CLAUDE_NOT_FOUND = "claude_not_found"
SPAWN_FAILED = "spawn_failed"
ERROR = "error"  # anything unexpected (a bug); `detail` names the exception


@dataclass(frozen=True)
class WarmupOutcome:
    profile_id: str
    trigger: str
    result: str  # succeeded | failed | skipped
    reason: str | None
    started_at: datetime
    finished_at: datetime
    resets_at: datetime | None = None
    rc: int | None = None
    detail: str | None = None


def build_command(profile: Profile, claude_path: str) -> list[str]:
    """The exact warm-up argv (ADR-0010; no shell)."""
    return [
        claude_path,
        "-p",
        profile.warmup.prompt,
        "--model",
        profile.warmup.model,
        "--no-session-persistence",
        "--settings",
        claude_cli.DISABLE_HOOKS_SETTINGS,
    ]


def warmup_env(profile: Profile, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The profile env (`CLAUDE_CONFIG_DIR`, parent session stripped) without any `CCS_*`."""
    env = claude_cli.profile_env(profile, base)
    return {k: v for k, v in env.items() if not k.startswith("CCS_")}


def stderr_tail(text: str, limit: int = STDERR_TAIL) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


Emit = Callable[[Event], Any]
Poll = Callable[[str], Awaitable[UsageSnapshot | None]]
Resolve = Callable[[], str]


class WarmupRunner:
    """Runs warm-ups; one at a time per profile."""

    def __init__(
        self,
        *,
        clock: Clock,
        store: WarmupStore,
        emit: Emit,
        poll: Poll,
        resolve_claude: Resolve,
        timeout_s: float = TIMEOUT_S,
        confirm_delays: Sequence[float] = CONFIRM_DELAYS_S,
    ) -> None:
        self.clock = clock
        self.store = store
        self._emit = emit
        self._poll = poll
        self._resolve = resolve_claude
        self.timeout_s = timeout_s
        self.confirm_delays = tuple(confirm_delays)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, profile_id: str) -> asyncio.Lock:
        lock = self._locks.get(profile_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[profile_id] = lock
        return lock

    async def _confirm_window(self, pid: str) -> UsageSnapshot | None:
        """Poll until the session window shows up (it can appear only after a delay)."""
        for delay in (0.0, *self.confirm_delays):
            if delay:
                log.info("warm-up %s: window not visible yet; polling again in %ss", pid, delay)
                await asyncio.sleep(delay)
            snap = await self._poll(pid)
            if session_window_active(snap, self.clock.now()):
                return snap
        return None

    def is_running(self, profile_id: str) -> bool:
        return self._lock(profile_id).locked()

    def skip(self, profile_id: str, trigger: str, reason: str) -> WarmupOutcome:
        """Record + emit a skip (no cooldown impact)."""
        now = self.clock.now()
        self.store.record_skip(profile_id, now, trigger, reason)
        data = {"trigger": trigger, "reason": reason}
        self._emit(Event("warmup.skipped", profile_id, None, data))
        return WarmupOutcome(profile_id, trigger, SKIPPED, reason, now, now)

    async def run(self, profile: Profile, trigger: str) -> WarmupOutcome:
        """Execute one warm-up for `profile` (callers evaluated the rules already)."""
        lock = self._lock(profile.id)
        if lock.locked():
            return self.skip(profile.id, trigger, rules.IN_PROGRESS)
        async with lock:
            return await self._run_locked(profile, trigger)

    async def _run_locked(self, profile: Profile, trigger: str) -> WarmupOutcome:
        pid = profile.id
        started = self.clock.now()
        self.store.begin_attempt(pid, started, trigger)
        self._emit(
            Event("warmup.started", pid, None, {"trigger": trigger, "model": profile.warmup.model})
        )
        rc: int | None = None
        detail: str | None = None
        resets_at: datetime | None = None
        try:
            claude = self._resolve()
            cwd = paths.warmup_cwd()
            cwd.mkdir(parents=True, exist_ok=True)
            done = await claude_cli.run(
                build_command(profile, claude),
                env=warmup_env(profile),
                cwd=str(cwd),
                timeout=self.timeout_s,
            )
            rc = done.rc
            if rc != 0:
                reason: str | None = f"exit_{rc}"
                detail = stderr_tail(done.stderr) or None
            else:
                snap = await self._confirm_window(pid)
                if snap is not None and snap.session is not None:
                    resets_at = snap.session.resets_at
                    reason = None
                else:
                    reason = WINDOW_NOT_STARTED
        except claude_cli.ClaudeTimeout:
            reason = TIMEOUT
        except claude_cli.ClaudeNotFound as exc:
            reason = CLAUDE_NOT_FOUND
            detail = str(exc)
        except OSError as exc:
            reason = SPAWN_FAILED
            detail = str(exc)
        except Exception as exc:  # the attempt is always finished and reported
            log.exception("warm-up %s (%s) raised", pid, trigger)
            reason = ERROR
            detail = f"{type(exc).__name__}: {exc}"
        finished = self.clock.now()
        result = SUCCEEDED if reason is None else FAILED
        self.store.finish_attempt(
            pid,
            at=started,
            trigger=trigger,
            result=result,
            reason=reason,
            resets_at=resets_at,
            finished_at=finished,
            detail=detail,
        )
        # full precision: two attempts in the same second must not dedupe each other's outcome
        key = f"warmup:{pid}:{started.isoformat(timespec='microseconds')}"
        if result == SUCCEEDED:
            log.info("warm-up %s (%s) succeeded; window resets %s", pid, trigger, resets_at)
            self._emit(
                Event(
                    "warmup.succeeded",
                    pid,
                    key,
                    {"trigger": trigger, "resets_at": format_iso(resets_at)},
                )
            )
        else:
            log.warning("warm-up %s (%s) failed: %s %s", pid, trigger, reason, detail or "")
            data: dict[str, Any] = {"trigger": trigger, "reason": reason}
            if detail:
                data["detail"] = detail
            self._emit(Event("warmup.failed", pid, key, data))
        return WarmupOutcome(pid, trigger, result, reason, started, finished, resets_at, rc, detail)
