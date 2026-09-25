"""The supervisor engine (P06): runs the pure policy inside the daemon and executes its actions.

- Registered as a daemon extension (`install(daemon)`), on the P04 hooks.
- State is persisted to `supervisor/<profile>.json` **before** commands are sent.
- Session records are updated immediately (the statusline reads them); `pause` / `resume`
  commands go to the launcher in background tasks — never awaited inside an op handler of
  the launcher's own connection (P05: that would deadlock until the ack timeout).
- At most one command is in flight per wrapper; that wrapper is left out of evaluations
  until its ack (or timeout) arrives, then the profile is re-evaluated.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ccs import fsio, paths
from ccs.config.models import Config, Profile
from ccs.daemon.hooks import WrapperInfo
from ccs.events import Event
from ccs.supervisor import ledger, policy
from ccs.supervisor.model import (
    MANUAL,
    OVERRIDDEN,
    PAUSED,
    RUNNING,
    Hold,
    ProfileSupervisorState,
    SessionView,
    bounded,
)
from ccs.usage.model import format_iso

if TYPE_CHECKING:
    from ccs.daemon.server import Conn, Daemon

log = logging.getLogger(__name__)

ACK_TIMEOUT_S = 75.0  # a pause can take lookup + typing guard (30 s) + verify (P05 notes)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class SupervisorEngine:
    """Policy evaluation, action execution, durability, and the `pause` / `resume` ops."""

    def __init__(self, daemon: Daemon, *, ack_timeout: float = ACK_TIMEOUT_S) -> None:
        self.daemon = daemon
        self.ack_timeout = ack_timeout
        self.states: dict[str, ProfileSupervisorState] = {}
        self._pending: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ install

    def install(self) -> None:
        hooks = self.daemon.hooks
        hooks.on_usage_updated(self.on_usage_updated)
        hooks.on_tick(self.on_tick)
        hooks.on_wrapper_registered(self.on_wrapper_registered)
        hooks.on_wrapper_unregistered(self.on_wrapper_unregistered)
        hooks.on_wrapper_event(self.on_wrapper_event)
        hooks.on_config_changed(self.on_config_changed)
        hooks.contribute_supervisor(self.contribute)
        hooks.handle_op("pause", self.op_pause)
        hooks.handle_op("resume", self.op_resume)

    # ------------------------------------------------------------ state

    def state(self, profile_id: str) -> ProfileSupervisorState:
        """The profile's state (loaded from disk on first use; confirmations restart)."""
        st = self.states.get(profile_id)
        if st is None:
            st = ProfileSupervisorState.from_dict(fsio.read_json(paths.supervisor_file(profile_id)))
            # durability: confirmation due-times are recomputed from `now`
            holds = tuple(
                dataclasses.replace(h, confirm_started_at=None, next_confirm_poll_at=None)
                for h in st.holds
            )
            st = dataclasses.replace(st, holds=holds)
            self.states[profile_id] = st
        return st

    def _save(self, profile_id: str, new: ProfileSupervisorState) -> None:
        old = self.states.get(profile_id)
        self.states[profile_id] = new
        if old == new:
            return
        try:
            fsio.atomic_write_json(paths.supervisor_file(profile_id), new.to_dict(profile_id))
        except OSError as exc:
            log.warning("cannot write supervisor state for %s: %s", profile_id, exc)
        if old is None or old.holds != new.holds:
            self.daemon.request_snapshot_write()

    # ------------------------------------------------------------ evaluation

    def _views(self, profile_id: str) -> list[SessionView]:
        views: list[SessionView] = []
        for rec in self.daemon.sessions_for(profile_id):
            view = SessionView.from_record(rec)
            if view is not None and not self._pending.get(view.wrapper_id):
                views.append(view)
        return views

    def _release_orphaned_holds(self, profile_id: str) -> None:
        """Session-scoped manual holds end with their session (ADR-0022).

        Also catches sessions that vanished without an unregister (records of dead launchers
        dropped at daemon start).
        """
        if self.daemon.profile(profile_id) is None:
            return
        st, dropped = policy.release_orphaned_holds(
            self.state(profile_id), self.daemon.clock.now(), self.daemon.sessions
        )
        if dropped:
            log.info("%s: session ended; released %s", profile_id, [h.instance for h in dropped])
            self._save(profile_id, st)  # persists and requests a snapshot write (holds changed)

    def evaluate(self, profile_id: str) -> policy.Decision | None:
        """Run the policy for one profile and execute the resulting actions."""
        profile = self.daemon.profile(profile_id)
        if profile is None:
            return None
        self._release_orphaned_holds(profile_id)
        now = self.daemon.clock.now()
        decision = policy.evaluate(
            profile,
            self.daemon.snapshots.get(profile_id),
            self.state(profile_id),
            self._views(profile_id),
            now,
        )
        self._save(profile_id, decision.state)  # persist before any command is sent
        for action in decision.actions:
            try:
                self._execute(profile, action, now)
            except Exception:
                log.exception("supervisor action %s failed", action)
        for event in decision.events:
            self.daemon.emit(event)
        return decision

    def _execute(self, profile: Profile, action: policy.Action, now: datetime) -> None:
        pid = profile.id
        if isinstance(action, policy.PauseSession):
            self._pause_session(pid, action, now)
        elif isinstance(action, policy.ResumeSession):
            self._resume_session(pid, action.wrapper_id, action.prompt)
        elif isinstance(action, policy.UpdateHolds):
            self._set_supervision(
                action.wrapper_id,
                holds=list(action.hold_ids),
                resume_at=format_iso(action.resume_at),
            )
        elif isinstance(action, policy.MarkRunning):
            self._set_supervision(
                action.wrapper_id,
                state=RUNNING,
                holds=[],
                paused_at=None,
                resume_at=None,
                overridden_instances=[],
            )
        elif isinstance(action, policy.AdoptOverride):
            self._adopt_override(pid, action)
        elif isinstance(action, policy.ForcePoll):
            at = action.at if action.at > now else None
            self.daemon.request_poll(pid, at)
        elif isinstance(action, policy.LogWarning):
            log.warning("%s", action.message)

    # ------------------------------------------------------------ session records

    def _set_supervision(self, wrapper_id: str, **fields: Any) -> dict[str, Any] | None:
        def mutate(rec: dict[str, Any]) -> None:
            sup = rec.get("supervision")
            if not isinstance(sup, dict):
                sup = {}
                rec["supervision"] = sup
            sup.update(fields)

        return self.daemon.update_session(wrapper_id, mutate)

    def _pause_session(self, pid: str, action: policy.PauseSession, now: datetime) -> None:
        rec = self.daemon.sessions.get(action.wrapper_id)
        if rec is None:
            return
        raw = rec.get("supervision")
        sup: dict[str, Any] = raw if isinstance(raw, dict) else {}
        was_paused = sup.get("state") == PAUSED
        self._set_supervision(
            action.wrapper_id,
            state=PAUSED,
            holds=list(action.hold_ids),
            paused_at=sup.get("paused_at") if was_paused else format_iso(now),
            resume_at=format_iso(action.resume_at),
            was_busy_at_pause=None,  # unknown until the launcher acks
        )
        payload = {"holds": list(action.hold_ids), "resume_at": format_iso(action.resume_at)}

        def on_ack(ack: dict[str, Any]) -> None:
            result = ack.get("result")
            detail = ack.get("detail") if isinstance(ack.get("detail"), dict) else {}
            busy = detail.get("was_busy") if isinstance(detail, dict) else None
            fields: dict[str, Any] = {}
            if result in ("injected", "skipped") and isinstance(busy, bool):
                fields["was_busy_at_pause"] = busy
            elif result in ("injected", "skipped"):
                # unknown status or an override during the pause: stays unknown (ADR-0022)
                log.debug("pause of %s: %s without was_busy", action.wrapper_id, result)
            else:
                log.warning("pause of %s: no usable ack (%s)", action.wrapper_id, result)
            session_id = _str(detail.get("session_id")) if isinstance(detail, dict) else None

            def mutate(rec: dict[str, Any]) -> None:
                sup = rec.get("supervision")
                if isinstance(sup, dict) and sup.get("state") == PAUSED:
                    sup.update(fields)
                if session_id:
                    rec["session_id"] = session_id

            self.daemon.update_session(action.wrapper_id, mutate)

        self._spawn_cmd(pid, action.wrapper_id, "pause", payload, on_ack)

    def _adopt_override(self, pid: str, action: policy.AdoptOverride) -> None:
        """Pending start-anyway instances now match the session's model → overridden."""
        rec = self.daemon.sessions.get(action.wrapper_id)
        if rec is None:
            return
        raw = rec.get("supervision")
        sup: dict[str, Any] = raw if isinstance(raw, dict) else {}
        previous = [i for i in sup.get("overridden_instances") or [] if isinstance(i, str)]
        pending = [
            i
            for i in sup.get("pending_override_instances") or []
            if isinstance(i, str) and i not in action.instances
        ]
        fields: dict[str, Any] = {
            "overridden_instances": list(bounded((*previous, *action.instances))),
            "pending_override_instances": pending,
        }
        if sup.get("state") != PAUSED:  # a pause for another hold keeps its state
            fields["state"] = OVERRIDDEN
        self._set_supervision(action.wrapper_id, **fields)
        self._record_override(pid, action.wrapper_id, action.instances, "started_overridden")

    def _resume_session(self, pid: str, wrapper_id: str, prompt: str | None) -> None:
        self._set_supervision(
            wrapper_id,
            state=RUNNING,
            holds=[],
            paused_at=None,
            resume_at=None,
            was_busy_at_pause=False,
        )

        def on_ack(ack: dict[str, Any]) -> None:
            log.info("resume of %s: %s %s", wrapper_id, ack.get("result"), ack.get("detail"))

        self._spawn_cmd(pid, wrapper_id, "resume", {"prompt": prompt}, on_ack)

    def _spawn_cmd(
        self,
        pid: str,
        wrapper_id: str,
        cmd_type: str,
        payload: dict[str, Any],
        on_ack: Callable[[dict[str, Any]], None],
    ) -> None:
        """Send one command in the background (serialized per wrapper), then re-evaluate."""
        self._pending[wrapper_id] = self._pending.get(wrapper_id, 0) + 1

        async def run() -> None:
            lock = self._locks.setdefault(wrapper_id, asyncio.Lock())
            try:
                async with lock:
                    ack = await self.daemon.send_cmd(
                        wrapper_id, cmd_type, payload, timeout=self.ack_timeout
                    )
                    on_ack(ack)
            finally:
                left = self._pending.get(wrapper_id, 1) - 1
                if left <= 0:
                    self._pending.pop(wrapper_id, None)
                else:
                    self._pending[wrapper_id] = left
                with contextlib.suppress(Exception):
                    self.evaluate(pid)

        task = asyncio.ensure_future(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def pending(self, wrapper_id: str) -> bool:
        """Whether a command to this wrapper is still in flight."""
        return bool(self._pending.get(wrapper_id))

    async def idle(self, timeout: float = 5.0) -> None:
        """Wait until no command is in flight (tests, shutdown)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ------------------------------------------------------------ hooks

    def on_usage_updated(self, profile_id: str) -> None:
        self.evaluate(profile_id)

    def on_tick(self, now: datetime) -> None:
        """Re-evaluate profiles that have holds or non-running sessions (confirmations)."""
        for pid in self.daemon.profile_ids():
            busy = bool(self.state(pid).holds) or any(
                isinstance(r.get("supervision"), dict) and r["supervision"].get("state") != RUNNING
                for r in self.daemon.sessions_for(pid)
            )
            if busy:
                self.evaluate(pid)

    def on_wrapper_registered(self, info: WrapperInfo) -> dict[str, Any]:
        rec = info.record
        pid = info.profile_id
        if info.started_overridden and not info.reregistered:
            view = SessionView.from_record(rec)
            holds = self.state(pid).holds
            inst = [h.instance for h in policy.applicable_holds(holds, view)] if view else []
            # model-scoped holds can't be matched before the statusline reports the model;
            # the start-anyway covers them too once the model is known (ADR-0022)
            pending = [h.instance for h in policy.undecided_holds(holds, view)] if view else []
            self._set_supervision(
                info.wrapper_id,
                state=OVERRIDDEN if inst else RUNNING,
                overridden_instances=inst,
                pending_override_instances=pending,
                holds=[],
                paused_at=None,
                resume_at=None,
            )
            if inst:
                self._record_override(pid, info.wrapper_id, inst, "started_overridden")
        self.evaluate(pid)
        sup = rec.get("supervision")
        return {"supervision": sup} if isinstance(sup, dict) else {}

    def on_wrapper_unregistered(self, info: WrapperInfo) -> None:
        """Unregister or reaper (`Daemon.unregister` drops the record before this hook)."""
        self._locks.pop(info.wrapper_id, None)
        self._release_orphaned_holds(info.profile_id)

    def on_wrapper_event(self, info: WrapperInfo, message: dict[str, Any]) -> None:
        if message.get("kind") != "input_submitted_while_paused":
            return
        rec = self.daemon.sessions.get(info.wrapper_id)
        if rec is None:
            return
        sup = rec.get("supervision")
        if not isinstance(sup, dict) or sup.get("state") != PAUSED:
            return
        view = SessionView.from_record(rec)
        holds = self.state(info.profile_id).holds
        current = [h.instance for h in policy.applicable_holds(holds, view)] if view else []
        previous = [i for i in sup.get("overridden_instances") or [] if isinstance(i, str)]
        inst = list(bounded((*previous, *current)))
        self._set_supervision(
            info.wrapper_id,
            state=OVERRIDDEN,
            overridden_instances=inst,
            holds=[],
            paused_at=None,
            resume_at=None,
        )
        self._record_override(info.profile_id, info.wrapper_id, current, "input_submitted")
        self.evaluate(info.profile_id)

    def _record_override(
        self, pid: str, wrapper_id: str, instances: Sequence[str], detail: str
    ) -> None:
        now = self.daemon.clock.now()
        st = self.state(pid)
        entry = ledger.entry(
            now,
            "override",
            instance=instances[0] if instances else None,
            wrapper_ids=[wrapper_id],
            detail=detail,
        )
        self._save(pid, dataclasses.replace(st, ledger=ledger.append(st.ledger, [entry])))
        self.daemon.emit(
            Event(
                "limit.override",
                pid,
                None,
                {"wrapper_id": wrapper_id, "instances": list(instances), "reason": detail},
            )
        )

    def on_config_changed(self, old: Config | None, new: Config) -> None:
        new_ids = {p.id for p in new.profiles}
        removed = [p.id for p in old.profiles if p.id not in new_ids] if old else []
        for pid in removed:
            self.states.pop(pid, None)
            with contextlib.suppress(FileNotFoundError):
                paths.supervisor_file(pid).unlink()
            for rec in list(self.daemon.sessions.values()):
                if rec.get("profile_id") != pid:
                    continue
                sup = rec.get("supervision")
                wid = _str(rec.get("wrapper_id"))
                if wid and isinstance(sup, dict) and sup.get("state") == PAUSED:
                    self._resume_session(pid, wid, None)
        for pid in new_ids:
            self.evaluate(pid)

    # ------------------------------------------------------------ contributions

    def contribute(self, profile_id: str) -> dict[str, Any]:
        """Snapshot / status `supervisor` fields: `state`, `holds`, `resume_at`."""
        profile = self.daemon.profile(profile_id)
        if profile is None:
            return {}
        holds = self.state(profile_id).holds
        label = policy.supervisor_state_label(
            profile, self.daemon.snapshots.get(profile_id), holds, self.daemon.clock.now()
        )
        return {
            "state": label,
            "holds": [h.public() for h in holds],
            "resume_at": format_iso(policy.profile_resume_at(holds)),
        }

    # ------------------------------------------------------------ ops

    def _target(self, msg: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        """`(profile_id, wrapper_id, error)` for a pause/resume request."""
        wid = _str(msg.get("wrapper_id"))
        pid = _str(msg.get("profile_id"))
        if wid is not None:
            rec = self.daemon.sessions.get(wid)
            if rec is None:
                return None, None, "unknown_session"
            return _str(rec.get("profile_id")), wid, None
        if pid is None:
            return None, None, "bad_request"
        if self.daemon.profile(pid) is None:
            return None, None, "unknown_profile"
        return pid, None, None

    async def op_pause(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        pid, wid, error = self._target(msg)
        if error or pid is None:
            return {"ok": False, "error": error or "unknown_profile"}
        profile = self.daemon.profile(pid)
        if profile is None:
            return {"ok": False, "error": "unknown_profile"}
        if not profile.supervisor.enabled:
            return {"ok": False, "error": "supervisor_disabled"}
        now = self.daemon.clock.now()
        st, hold, created = policy.add_manual_hold(self.state(pid), now, wid)
        self._save(pid, st)
        decision = self.evaluate(pid)
        paused = sum(
            1
            for a in (decision.actions if decision else ())
            if isinstance(a, policy.PauseSession) and hold.instance in a.instances
        )
        if created:
            self.daemon.emit(
                Event(
                    "limit.pause",
                    pid,
                    f"{pid}:pause:{hold.instance}",
                    {
                        "window": MANUAL,
                        "name": None,
                        "hold": MANUAL,
                        "manual": True,
                        "wrapper_id": wid,
                        "percent": None,
                        "resume_at": None,
                        "sessions_paused": paused,
                    },
                )
            )
        return {
            "ok": True,
            "profile_id": pid,
            "wrapper_id": wid,
            "created": created,
            "hold": hold.public(),
            "sessions_paused": paused,
        }

    async def op_resume(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        pid, wid, error = self._target(msg)
        if error or pid is None:
            return {"ok": False, "error": error or "unknown_profile"}
        profile = self.daemon.profile(pid)
        if profile is None:
            return {"ok": False, "error": "unknown_profile"}
        now = self.daemon.clock.now()
        if wid is not None:
            return self._resume_wrapper(profile, wid, now)
        st, cleared = policy.release_holds(self.state(pid), now)
        self._save(pid, st)
        decision = self.evaluate(pid)
        resumed = sum(
            1 for a in (decision.actions if decision else ()) if isinstance(a, policy.ResumeSession)
        )
        if cleared or resumed:
            self.daemon.emit(
                Event(
                    "limit.resume",
                    pid,
                    f"{pid}:resume:manual:{format_iso(now)}",
                    {
                        "window": MANUAL,
                        "manual": True,
                        "reason": "manual",
                        "holds": [h.id for h in cleared],
                        "sessions_resumed": resumed,
                    },
                )
            )
        return {
            "ok": True,
            "profile_id": pid,
            "wrapper_id": None,
            "cleared": [h.public() for h in cleared],
            "sessions_resumed": resumed,
        }

    def _resume_wrapper(self, profile: Profile, wid: str, now: datetime) -> dict[str, Any]:
        pid = profile.id
        st = self.state(pid)
        keep = [h for h in st.holds if not (h.kind == MANUAL and h.scope == wid)]
        st, cleared = policy.release_holds(st, now, keep=keep, wrapper_id=wid)
        self._save(pid, st)
        rec = self.daemon.sessions.get(wid)
        resumed = 0
        if rec is not None:
            view = SessionView.from_record(rec)
            sup = rec.get("supervision") if isinstance(rec.get("supervision"), dict) else {}
            remaining: list[Hold] = policy.applicable_holds(st.holds, view) if view else []
            inst = [h.instance for h in remaining]
            if isinstance(sup, dict) and sup.get("state") == PAUSED:
                busy = sup.get("was_busy_at_pause") is True
                prompt = profile.supervisor.resume_prompt if busy else None
                previous = [i for i in sup.get("overridden_instances") or [] if isinstance(i, str)]
                self._set_supervision(
                    wid,
                    state=OVERRIDDEN if inst else RUNNING,
                    overridden_instances=list(bounded((*previous, *inst))),
                    holds=[],
                    paused_at=None,
                    resume_at=None,
                    was_busy_at_pause=False,
                )
                entry = ledger.entry(
                    now,
                    "manual_resume",
                    wrapper_ids=[wid],
                    detail="prompt" if prompt else "no_prompt",
                )
                st = self.state(pid)
                self._save(pid, dataclasses.replace(st, ledger=ledger.append(st.ledger, [entry])))
                self._spawn_cmd(pid, wid, "resume", {"prompt": prompt}, lambda ack: None)
                resumed = 1
        if cleared or resumed:
            self.daemon.emit(
                Event(
                    "limit.resume",
                    pid,
                    f"{pid}:resume:{wid}:{format_iso(now)}",
                    {
                        "window": MANUAL,
                        "manual": True,
                        "reason": "manual",
                        "wrapper_id": wid,
                        "holds": [h.id for h in cleared],
                        "sessions_resumed": resumed,
                    },
                )
            )
        return {
            "ok": True,
            "profile_id": pid,
            "wrapper_id": wid,
            "cleared": [h.public() for h in cleared],
            "sessions_resumed": resumed,
        }


def install(daemon: Daemon) -> SupervisorEngine:
    """Daemon extension entry point (`ccs.daemon.extensions.EXTENSIONS`)."""
    engine = SupervisorEngine(daemon)
    engine.install()
    return engine
