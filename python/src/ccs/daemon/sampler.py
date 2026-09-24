"""`claude agents --json` sampling: activity of supervised sessions + counts of other sessions.

Field names (P00-S3): every entry has `kind` (`interactive` | `background`), `sessionId`,
`startedAt`, `cwd`, `name`; `pid` and `status` (`busy|idle|shell|waiting`) only while
running; background entries also carry `id` and `state`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ccs import claude_cli

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)

FAST_S = 15.0
SLOW_S = 60.0
STATUSES = ("busy", "idle", "shell", "waiting")


@dataclass(frozen=True)
class AgentsSample:
    """Result of classifying one `agents --json` list."""

    activity: dict[str, tuple[str, str | None]]  # wrapper_id -> (status, session_id)
    counts: dict[str, int]  # {"interactive": n, "background": n}


def classify_agents(
    entries: Sequence[Mapping[str, Any]],
    wrappers_by_claude_pid: Mapping[int, str],
    own_pids: set[int],
) -> AgentsSample:
    """Map supervised sessions' activity; count everything else by kind (pure).

    Our own probe / warm-up children are skipped; unknown kinds count as background.
    """
    activity: dict[str, tuple[str, str | None]] = {}
    counts = {"interactive": 0, "background": 0}
    for entry in entries:
        pid = entry.get("pid")
        pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        if pid is not None and pid in own_pids:
            continue
        if pid is not None and pid in wrappers_by_claude_pid:
            status = entry.get("status")
            sid = entry.get("sessionId")
            activity[wrappers_by_claude_pid[pid]] = (
                status if status in STATUSES else "unknown",
                sid if isinstance(sid, str) and sid else None,
            )
            continue
        kind = "interactive" if entry.get("kind") == "interactive" else "background"
        counts[kind] += 1
    return AgentsSample(activity, counts)


def _updater(updates: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def apply(rec: dict[str, Any]) -> None:
        rec.update(updates)

    return apply


class Sampler:
    """Samples every profile: every 15 s with supervised sessions, else every 60 s."""

    def __init__(self, daemon: Daemon, *, fast_s: float = FAST_S, slow_s: float = SLOW_S) -> None:
        self.daemon = daemon
        self.fast_s = fast_s
        self.slow_s = slow_s
        self._next: dict[str, float] = {}

    async def sample_profile(self, profile_id: str) -> AgentsSample | None:
        daemon = self.daemon
        profile = daemon.profile(profile_id)
        if profile is None:
            return None
        try:
            claude = claude_cli.resolve_claude(daemon.config)
            entries = await claude_cli.agents_json(claude, profile)
        except claude_cli.ClaudeCliError as exc:
            log.info("agents sample for %s failed: %s", profile_id, exc)
            return None
        wrappers: dict[int, str] = {}
        for rec in daemon.sessions_for(profile_id):
            cpid = rec.get("claude_pid")
            if isinstance(cpid, int) and not isinstance(cpid, bool):
                wrappers[cpid] = str(rec["wrapper_id"])
        sample = classify_agents(entries, wrappers, claude_cli.spawned_pids())
        changed = daemon.other_sessions.get(profile_id) != sample.counts
        daemon.other_sessions[profile_id] = sample.counts
        for wrapper_id, (status, session_id) in sample.activity.items():
            current = daemon.sessions.get(wrapper_id)
            if current is None:
                continue
            updates: dict[str, Any] = {}
            if current.get("activity") != status:
                updates["activity"] = status
            if session_id and current.get("session_id") != session_id:
                updates["session_id"] = session_id
            if updates:
                daemon.update_session(wrapper_id, _updater(updates))
        if changed:
            daemon.request_snapshot_write()
        return sample

    async def loop(self) -> None:
        while True:
            now = time.monotonic()
            for pid in self.daemon.profile_ids():
                if now < self._next.get(pid, 0.0):
                    continue
                period = self.fast_s if self.daemon.sessions_for(pid) else self.slow_s
                self._next[pid] = now + period
                try:
                    await self.sample_profile(pid)
                except Exception:
                    log.exception("agents sample for %s failed", pid)
            await asyncio.sleep(1.0)
