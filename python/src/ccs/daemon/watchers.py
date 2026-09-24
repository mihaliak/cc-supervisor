"""File watchers: statusline live reports (1 s) and the config file (2 s). Stdlib mtime polling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ccs import fsio, paths
from ccs.config import store
from ccs.events import Event
from ccs.usage.merge import parse_live_report

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)

LIVE_GC_S = 24 * 3600


class LiveWatcher:
    """Tracks `live/*.json`; re-merges the affected profiles when a report changes."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self._stats: dict[str, tuple[int, int]] = {}

    async def scan_once(self) -> set[str]:
        """One scan; returns the profile ids whose reports changed."""
        daemon = self.daemon
        live_dir = paths.live_dir()
        try:
            entries = list(live_dir.glob("*.json"))
        except OSError:
            entries = []
        affected: set[str] = set()
        present: set[str] = set()
        now_wall = time.time()
        for path in entries:
            name = path.name
            try:
                st = path.stat()
            except OSError:
                continue
            if now_wall - st.st_mtime > LIVE_GC_S:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            present.add(name)
            key = (st.st_mtime_ns, st.st_size)
            if self._stats.get(name) == key:
                continue
            self._stats[name] = key
            report = parse_live_report(fsio.read_json(path))
            if report is None:
                continue
            old = daemon.live_reports.get(name)
            daemon.live_reports[name] = report
            for pid in (report.profile_id, old.profile_id if old else None):
                if pid:
                    affected.add(pid)
            self._update_session(report.wrapper_id, report.session_id, report.model_id)
        for name in set(self._stats) - present:
            self._stats.pop(name, None)
            gone = daemon.live_reports.pop(name, None)
            if gone is not None and gone.profile_id:
                affected.add(gone.profile_id)
        for pid in sorted(affected):
            if daemon.profile(pid) is not None:
                await daemon.apply_usage(pid)
        return affected

    def _update_session(
        self, wrapper_id: str | None, session_id: str | None, model_id: str | None
    ) -> None:
        if not wrapper_id or wrapper_id not in self.daemon.sessions:
            return
        rec = self.daemon.sessions[wrapper_id]
        changes: dict[str, Any] = {}
        if session_id and rec.get("session_id") != session_id:
            changes["session_id"] = session_id
        if model_id and rec.get("model_id") != model_id:
            changes["model_id"] = model_id
        if changes:
            self.daemon.update_session(wrapper_id, lambda r: r.update(changes))

    async def loop(self, interval: float = 1.0) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception:
                log.exception("live report scan failed")
            await asyncio.sleep(interval)


class ConfigWatcher:
    """Reloads `config.json` on change; keeps the last good config when it turns invalid."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self._stat: tuple[int, int] | None = None

    def _current_stat(self) -> tuple[int, int] | None:
        try:
            st = Path(self.daemon.config_path).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def mark_seen(self) -> None:
        """Remember the current file state (the daemon just loaded it)."""
        self._stat = self._current_stat()

    async def check_once(self, *, force: bool = False) -> dict[str, Any]:
        """Reload when changed (or `force`). Returns `{ok, revision?, issues?, changed}`."""
        current = self._current_stat()
        if not force and current == self._stat:
            return {"ok": True, "changed": False}
        self._stat = current
        path = self.daemon.config_path
        try:
            cfg, _raw = store.load(path)
        except store.ConfigMissing:
            log.warning("config file missing: %s (keeping the last good config)", path)
            return {"ok": False, "changed": False, "issues": [{"path": "", "message": "missing"}]}
        except store.ConfigInvalid as exc:
            issues = [{"path": i.path, "message": i.message} for i in exc.issues]
            revision = _revision(path)
            log.warning("config invalid (keeping the last good config): %s", exc)
            self.daemon.emit(
                Event(
                    "config.invalid",
                    None,
                    f"config_invalid:{revision if revision is not None else current}",
                    {"issues": issues, "revision": revision},
                )
            )
            return {"ok": False, "changed": False, "issues": issues, "revision": revision}
        old = self.daemon.config
        if old is not None and old.to_dict() == cfg.to_dict():
            return {"ok": True, "changed": False, "revision": cfg.revision}
        log.info("config reloaded (revision %s)", cfg.revision)
        await self.daemon.set_config(cfg)
        return {"ok": True, "changed": True, "revision": cfg.revision, "issues": []}

    async def loop(self, interval: float = 2.0) -> None:
        if self._stat is None:
            self.mark_seen()
        while True:
            await asyncio.sleep(interval)
            try:
                await self.check_once()
            except Exception:
                log.exception("config check failed")


def _revision(path: Path) -> int | None:
    data = fsio.read_json(path)
    rev = data.get("revision") if data else None
    return rev if isinstance(rev, int) and not isinstance(rev, bool) else None
