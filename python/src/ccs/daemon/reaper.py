"""Removes session records whose launcher process died (crash, `kill -9`, closed terminal)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, TypeGuard

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)

MAX_PID = 2**31 - 1  # pids arrive over the socket; `os.kill` needs a C int (ADR-0022)
# Records loaded from disk at daemon start are provisional: their launcher must re-register
# within this grace or the record is reaped as `stale`, even with a live pid (pids get
# reused, ADR-0022). A live launcher retries at least every 10 s (the last step of
# `launcher.daemon_link.BACKOFF_S`), so this is 3x that, but at least a minute.
REREGISTER_GRACE_S = 60.0


def valid_pid(value: Any) -> TypeGuard[int]:
    """True for a pid the daemon accepts: an int in `1..MAX_PID`."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_PID


def pid_alive(pid: int) -> bool:
    """True when a process with `pid` exists (EPERM counts as alive; an impossible pid is dead)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return False
    return True


class Reaper:
    """Every `interval` seconds, unregister wrappers whose `wrapper_pid` is gone, and records
    loaded at startup whose launcher never re-registered."""

    def __init__(self, daemon: Daemon, *, grace_s: float = REREGISTER_GRACE_S) -> None:
        self.daemon = daemon
        self.grace_s = grace_s

    def reason(self, wrapper_id: str, rec: dict[str, Any]) -> str | None:
        """`reaped` (launcher gone), `stale` (never re-registered after a restart), or `None`."""
        wpid = rec.get("wrapper_pid")
        if not (valid_pid(wpid) and pid_alive(wpid)):
            return "reaped"
        loaded = self.daemon.provisional.get(wrapper_id)
        if loaded is not None and self.daemon.clock.monotonic() - loaded >= self.grace_s:
            return "stale"
        return None

    async def reap_once(self) -> list[str]:
        reaped: list[str] = []
        for wrapper_id, rec in list(self.daemon.sessions.items()):
            try:
                reason = self.reason(wrapper_id, rec)
                if reason is None:
                    continue
                log.info(
                    "reaping wrapper %s (%s, pid %s)", wrapper_id, reason, rec.get("wrapper_pid")
                )
                await self.daemon.unregister(wrapper_id, exit_code=None, reason=reason)
                reaped.append(wrapper_id)
            except Exception:  # one bad record must not stop reaping the others
                log.exception("reaping wrapper %s failed", wrapper_id)
        return reaped

    async def loop(self, interval: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_once()
            except Exception:
                log.exception("reaper failed")
