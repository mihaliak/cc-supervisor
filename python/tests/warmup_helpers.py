"""Helpers for warm-up tests: configs, snapshots, a fake daemon host and a stub runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ccs.clock import FakeClock
from ccs.config.models import Config, Profile
from ccs.daemon.hooks import DaemonHooks
from ccs.events import Event
from ccs.usage.model import UsageSnapshot, Window

TZ = ZoneInfo("Europe/Bratislava")


def local(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    """A Bratislava wall-clock time as tz-aware UTC."""
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ).astimezone(UTC)


def warmup(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "model": "haiku",
        "prompt": "Reply with just: ok",
        "triggers": {
            "schedule": [],
            "app_start": True,
            "unlock_wake": True,
            "auto_chain": True,
        },
        "active_hours": {"start": "07:00", "end": "23:00"},
        "cooldown_minutes": 10,
    }
    triggers = over.pop("triggers", None)
    base.update(over)
    if triggers:
        base["triggers"] = {**base["triggers"], **triggers}
    return base


def config(*profiles: dict[str, Any]) -> Config:
    raw_profiles = []
    for p in profiles or ({"id": "work"},):
        pid = p["id"]
        raw: dict[str, Any] = {
            "id": pid,
            "flag": pid,
            "name": pid.title(),
            "emoji": "💼",
            "config_dir": f"/tmp/ccs-test-{pid}",
            "warmup": p.get("warmup", warmup()),
        }
        raw_profiles.append(raw)
    return Config.from_dict({"version": 1, "revision": 0, "profiles": raw_profiles})


def profile(**warmup_over: Any) -> Profile:
    cfg = config({"id": "work", "warmup": warmup(**warmup_over)})
    return cfg.profiles[0]


def snapshot(
    *,
    status: str = "ok",
    resets_at: datetime | None = None,
    percent: int = 5,
    observed: datetime | None = None,
    session: bool = True,
) -> UsageSnapshot:
    when = observed or datetime(2026, 9, 24, 12, tzinfo=UTC)
    return UsageSnapshot(
        profile_id="work",
        status=status,
        fetched_at=when,
        polled_at=when,
        session=Window(percent, resets_at, when) if session else None,
    )


class FakeDaemon:
    """Just enough of `Daemon` for the scheduler."""

    def __init__(self, cfg: Config, clock: FakeClock) -> None:
        self.config: Config | None = cfg
        self.clock = clock
        self.hooks = DaemonHooks()
        self.snapshots: dict[str, UsageSnapshot] = {}
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.poll_results: dict[str, list[UsageSnapshot | None]] = {}
        self.polls: list[str] = []
        self.events: list[Event] = []
        self.snapshot_writes = 0
        self.supervisor: dict[str, dict[str, Any]] = {}

    def profile(self, pid: str) -> Profile | None:
        return self.config.profile(pid) if self.config else None

    def sessions_for(self, pid: str) -> list[dict[str, Any]]:
        return self.sessions.get(pid, [])

    def supervisor_state(self, pid: str) -> dict[str, Any]:
        return self.supervisor.get(pid, {})

    def request_poll(self, pid: str, at: datetime | None = None) -> asyncio.Future[Any]:
        self.polls.append(pid)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        queue = self.poll_results.get(pid)
        result = queue.pop(0) if queue else self.snapshots.get(pid)
        if result is not None:
            self.snapshots[pid] = result
        fut.set_result(result)
        return fut

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def request_snapshot_write(self) -> None:
        self.snapshot_writes += 1

    def add_task(self, factory: Any) -> None:
        pass


class StubRunner:
    """Records runs and skips instead of executing claude."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []
        self.skips: list[tuple[str, str, str]] = []
        self.running: set[str] = set()

    def is_running(self, pid: str) -> bool:
        return pid in self.running

    def skip(self, pid: str, trigger: str, reason: str) -> None:
        self.skips.append((pid, trigger, reason))

    async def run(self, prof: Profile, trigger: str) -> None:
        self.runs.append((prof.id, trigger))


async def settle() -> None:
    """Let spawned tasks run."""
    for _ in range(5):
        await asyncio.sleep(0)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
