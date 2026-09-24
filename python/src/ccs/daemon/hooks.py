"""Daemon extension points (P04). Later plans register callbacks instead of editing the core.

Every callback may be sync or async; failures are logged and never stop the daemon.
Extensions are modules listed in `ccs.daemon.extensions.EXTENSIONS` exposing
`install(daemon)`, which calls the `on_*` registrars below and `daemon.add_task(...)`.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ccs.config.models import Config

if TYPE_CHECKING:
    from ccs.daemon.server import Conn

log = logging.getLogger(__name__)

MaybeAwaitable = Any  # a value or an awaitable of it

UsageUpdated = Callable[[str], MaybeAwaitable]
Tick = Callable[[datetime], MaybeAwaitable]
WrapperHook = Callable[["WrapperInfo"], MaybeAwaitable]
WrapperEventHook = Callable[["WrapperInfo", dict[str, Any]], MaybeAwaitable]
ConfigChanged = Callable[[Config | None, Config], MaybeAwaitable]
OpHandler = Callable[["Conn", dict[str, Any]], Awaitable[dict[str, Any]]]
SupervisorContributor = Callable[[str], Mapping[str, Any] | None]


@dataclass
class WrapperInfo:
    """A launcher registration as seen by hooks (the record is the session file content)."""

    wrapper_id: str
    profile_id: str
    wrapper_pid: int | None
    claude_pid: int | None
    cwd: str | None
    started_overridden: bool
    reregistered: bool
    record: dict[str, Any]


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


@dataclass
class DaemonHooks:
    """Registry of callbacks; `fire_*` run them in registration order."""

    usage_updated: list[UsageUpdated] = field(default_factory=list)
    tick: list[Tick] = field(default_factory=list)
    wrapper_registered: list[WrapperHook] = field(default_factory=list)
    wrapper_unregistered: list[WrapperHook] = field(default_factory=list)
    wrapper_event: list[WrapperEventHook] = field(default_factory=list)
    config_changed: list[ConfigChanged] = field(default_factory=list)
    supervisor_contributors: list[SupervisorContributor] = field(default_factory=list)
    ops: dict[str, OpHandler] = field(default_factory=dict)

    # -- registrars

    def on_usage_updated(self, fn: UsageUpdated) -> None:
        """`fn(profile_id)` after the merged usage of a profile changed (poll or live report)."""
        self.usage_updated.append(fn)

    def on_tick(self, fn: Tick) -> None:
        """`fn(now)` about once per second."""
        self.tick.append(fn)

    def on_wrapper_registered(self, fn: WrapperHook) -> None:
        """`fn(info) -> dict | None`: returned dicts are merged into the register reply."""
        self.wrapper_registered.append(fn)

    def on_wrapper_unregistered(self, fn: WrapperHook) -> None:
        """`fn(info)` after a launcher unregistered or was reaped."""
        self.wrapper_unregistered.append(fn)

    def on_wrapper_event(self, fn: WrapperEventHook) -> None:
        """`fn(info, message)` for each `wrapper_event`."""
        self.wrapper_event.append(fn)

    def on_config_changed(self, fn: ConfigChanged) -> None:
        """`fn(old, new)` after a valid config was loaded (old is `None` at startup)."""
        self.config_changed.append(fn)

    def contribute_supervisor(self, fn: SupervisorContributor) -> None:
        """`fn(profile_id) -> dict`: overrides for the profile's `supervisor` fields."""
        self.supervisor_contributors.append(fn)

    def handle_op(self, op: str, handler: OpHandler) -> None:
        """Serve socket op `op` (e.g. `pause`, `resume`, `warmup`)."""
        self.ops[op] = handler

    # -- firing

    async def fire_usage_updated(self, profile_id: str) -> None:
        for fn in list(self.usage_updated):
            try:
                await _call(fn, profile_id)
            except Exception:
                log.exception("usage_updated hook failed")

    async def fire_tick(self, now: datetime) -> None:
        for fn in list(self.tick):
            try:
                await _call(fn, now)
            except Exception:
                log.exception("tick hook failed")

    async def fire_wrapper_registered(self, info: WrapperInfo) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for fn in list(self.wrapper_registered):
            try:
                extra = await _call(fn, info)
            except Exception:
                log.exception("wrapper_registered hook failed")
                continue
            if isinstance(extra, Mapping):
                merged.update(extra)
        return merged

    async def fire_wrapper_unregistered(self, info: WrapperInfo) -> None:
        for fn in list(self.wrapper_unregistered):
            try:
                await _call(fn, info)
            except Exception:
                log.exception("wrapper_unregistered hook failed")

    async def fire_wrapper_event(self, info: WrapperInfo, message: dict[str, Any]) -> None:
        for fn in list(self.wrapper_event):
            try:
                await _call(fn, info, message)
            except Exception:
                log.exception("wrapper_event hook failed")

    async def fire_config_changed(self, old: Config | None, new: Config) -> None:
        for fn in list(self.config_changed):
            try:
                await _call(fn, old, new)
            except Exception:
                log.exception("config_changed hook failed")

    def supervisor_fields(self, profile_id: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for fn in list(self.supervisor_contributors):
            try:
                extra = fn(profile_id)
            except Exception:
                log.exception("supervisor contributor failed")
                continue
            if isinstance(extra, Mapping):
                merged.update(extra)
        return merged
