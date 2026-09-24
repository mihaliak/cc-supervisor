"""Removes session records whose launcher process died (crash, `kill -9`, closed terminal)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)


def pid_alive(pid: int) -> bool:
    """True when a process with `pid` exists (EPERM counts as alive)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Reaper:
    """Every `interval` seconds, unregister wrappers whose `wrapper_pid` is gone."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    async def reap_once(self) -> list[str]:
        reaped: list[str] = []
        for wrapper_id, rec in list(self.daemon.sessions.items()):
            wpid = rec.get("wrapper_pid")
            if isinstance(wpid, int) and not isinstance(wpid, bool) and pid_alive(wpid):
                continue
            log.info("reaping wrapper %s (pid %s gone)", wrapper_id, wpid)
            await self.daemon.unregister(wrapper_id, exit_code=None, reason="reaped")
            reaped.append(wrapper_id)
        return reaped

    async def loop(self, interval: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_once()
            except Exception:
                log.exception("reaper failed")
