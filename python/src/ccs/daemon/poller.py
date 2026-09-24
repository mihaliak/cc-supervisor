"""Per-profile usage polling at the ADR-0008 cadence, with coalesced forced polls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ccs.config.models import Config, Polling
from ccs.events import Event, hour_bucket
from ccs.usage.model import (
    STATUS_NEEDS_SIGN_IN,
    STATUS_SOURCE_ERROR,
    UsageSnapshot,
)
from ccs.usage.source_claude import fetch_snapshot

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)

MAX_WAIT_S = 1.0  # loops re-evaluate at least this often (cheap; keeps them responsive)


def max_percent(snapshot: UsageSnapshot | None) -> int | None:
    """Highest percent across session, weekly and model-scoped windows."""
    if snapshot is None:
        return None
    values = [w.percent for w in (snapshot.session, snapshot.weekly) if w is not None]
    values += [w.percent for w in snapshot.model_scoped]
    return max(values) if values else None


def interval_for(
    polling: Polling, snapshot: UsageSnapshot | None, activities: Sequence[str]
) -> int:
    """Seconds until the next regular poll (pure, ADR-0008).

    - no registered wrappers for the profile → `idle_interval_seconds`
    - any window ≥ `fast_when_percent_at_least` and a wrapper not `idle` → `fast_interval_seconds`
    - otherwise `interval_seconds`
    """
    if not activities:
        return polling.idle_interval_seconds
    top = max_percent(snapshot)
    busy = any(a != "idle" for a in activities)
    if top is not None and top >= polling.fast_when_percent_at_least and busy:
        return polling.fast_interval_seconds
    return polling.interval_seconds


@dataclass
class _Waiter:
    at: datetime | None
    future: asyncio.Future[UsageSnapshot | None]


@dataclass
class _ProfileState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    waiters: list[_Waiter] = field(default_factory=list)
    last_poll_at: datetime | None = None
    last_status: str | None = None
    task: asyncio.Task[None] | None = None
    probes: int = 0


class Poller:
    """Owns one `poll_loop` task per profile."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self._states: dict[str, _ProfileState] = {}
        self._stopping = False

    def _state(self, profile_id: str) -> _ProfileState:
        st = self._states.get(profile_id)
        if st is None:
            st = _ProfileState()
            self._states[profile_id] = st
        return st

    # -- lifecycle

    def sync(self, old: Config | None, new: Config) -> None:
        """Start loops for new profiles, stop removed ones, force-poll changed ones."""
        old_dirs = {p.id: p.config_dir for p in old.profiles} if old else {}
        new_ids = {p.id for p in new.profiles}
        for pid in list(self._states):
            if pid not in new_ids:
                self._stop(pid)
        for p in new.profiles:
            st = self._state(p.id)
            if st.task is None or st.task.done():
                st.task = asyncio.ensure_future(self._loop(p.id))
            if old is not None and old_dirs.get(p.id) not in (None, p.config_dir):
                self.request_poll(p.id)  # config dir changed: the old data is someone else's

    def _stop(self, profile_id: str) -> None:
        st = self._states.pop(profile_id, None)
        if st is None:
            return
        if st.task is not None:
            st.task.cancel()
        for w in st.waiters:
            if not w.future.done():
                w.future.set_result(None)

    def stop_all(self) -> None:
        for pid in list(self._states):
            self._stop(pid)

    async def shutdown(self, timeout: float = 20.0) -> None:
        """Let in-flight probes finish (their children exit cleanly), then stop every loop."""
        self._stopping = True
        tasks = [st.task for st in self._states.values() if st.task is not None]
        for st in self._states.values():
            st.wake.set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.stop_all()

    # -- forced polls

    def request_poll(
        self, profile_id: str, at: datetime | None = None
    ) -> asyncio.Future[UsageSnapshot | None]:
        """Poll soon (or at `at`); resolves with the merged snapshot of that poll.

        Requests made while a probe is in flight are served by one follow-up probe.
        """
        fut: asyncio.Future[UsageSnapshot | None] = asyncio.get_running_loop().create_future()
        if self._stopping or self.daemon.profile(profile_id) is None:
            fut.set_result(None)
            return fut
        st = self._state(profile_id)
        st.waiters.append(_Waiter(at, fut))
        st.wake.set()
        return fut

    def probe_count(self, profile_id: str) -> int:
        st = self._states.get(profile_id)
        return st.probes if st else 0

    # -- loop

    def _activities(self, profile_id: str) -> list[str]:
        return [
            str(rec.get("activity") or "unknown") for rec in self.daemon.sessions_for(profile_id)
        ]

    async def _loop(self, profile_id: str) -> None:
        st = self._state(profile_id)
        while not self._stopping:
            cfg = self.daemon.config
            if cfg is None or cfg.profile(profile_id) is None:
                return
            now = self.daemon.clock.now()
            interval = interval_for(
                cfg.polling, self.daemon.snapshots.get(profile_id), self._activities(profile_id)
            )
            due = now if st.last_poll_at is None else st.last_poll_at + timedelta(seconds=interval)
            forced = [w.at or now for w in st.waiters]
            next_at = min([due, *forced])
            delay = (next_at - now).total_seconds()
            if delay > 0:
                st.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(st.wake.wait(), min(delay, MAX_WAIT_S))
                continue
            batch = [w for w in st.waiters if w.at is None or w.at <= now]
            st.waiters = [w for w in st.waiters if not (w.at is None or w.at <= now)]
            try:
                snap = await self.poll_once(profile_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll of %s failed", profile_id)
                snap = None
                st.last_poll_at = self.daemon.clock.now()
            for w in batch:
                if not w.future.done():
                    w.future.set_result(snap)

    async def poll_once(self, profile_id: str) -> UsageSnapshot | None:
        """One probe: fetch → store → merge → write usage file (always) → events → hooks."""
        st = self._state(profile_id)
        async with st.lock:
            daemon = self.daemon
            cfg = daemon.config
            profile = daemon.profile(profile_id)
            if cfg is None or profile is None:
                return None
            previous = daemon.polled.get(profile_id) or daemon.snapshots.get(profile_id)
            started = time.monotonic()
            snap = await fetch_snapshot(cfg, profile, previous=previous, clock=daemon.clock)
            st.probes += 1
            st.last_poll_at = daemon.clock.now()
            log.info(
                "poll %s: %s in %.2fs%s",
                profile_id,
                snap.status,
                time.monotonic() - started,
                f" ({snap.error})" if snap.error else "",
            )
            daemon.polled[profile_id] = snap
            self._transition_events(profile_id, st, snap)
            await daemon.apply_usage(profile_id, force_write=True)
            return daemon.snapshots.get(profile_id)

    def _transition_events(self, profile_id: str, st: _ProfileState, snap: UsageSnapshot) -> None:
        old, st.last_status = st.last_status, snap.status
        if snap.status == old:
            return
        bucket = hour_bucket(self.daemon.clock.now())
        if snap.status == STATUS_NEEDS_SIGN_IN:
            self.daemon.emit(Event("auth.required", profile_id, f"auth:{profile_id}:{bucket}", {}))
        elif snap.status == STATUS_SOURCE_ERROR:
            self.daemon.emit(
                Event(
                    "usage.source_error",
                    profile_id,
                    f"source_error:{profile_id}:{bucket}",
                    {"error": snap.error},
                )
            )
