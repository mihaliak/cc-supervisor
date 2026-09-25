"""The daemon: one asyncio loop serving the socket and running pollers/watchers (ADR-0005/0006).

The daemon is the single writer of `sessions/*.json`, `usage/*.json`, `supervisor/*.json`,
`widget/snapshot.json` and `events.jsonl`. Later plans plug in through `DaemonHooks`
(`self.hooks`) and `ccs.daemon.extensions`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from ccs import __version__, fsio, paths
from ccs.clock import Clock, SystemClock
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.daemon import extensions as ext_mod
from ccs.daemon.hooks import DaemonHooks, WrapperInfo
from ccs.events import TEST_TYPE, Event, EventBus
from ccs.snapshot import build_widget_snapshot, write_widget_snapshot
from ccs.usage.merge import LiveReport, apply_staleness, merge
from ccs.usage.model import UsageSnapshot, format_iso
from ccs.usage.source_claude import read_snapshot, write_snapshot

log = logging.getLogger(__name__)

PROTO = 1
LINE_LIMIT = 1024 * 1024
ACK_TIMEOUT_S = 10.0
QUEUE_MAX = 1000
SNAPSHOT_MIN_INTERVAL_S = 1.0
SESSION_SCHEMA = 1
ACTIVITIES = ("busy", "idle", "shell", "waiting", "unknown")


class DaemonAlreadyRunning(Exception):
    """Another daemon holds `daemon.lock`."""


def new_session_record(
    *,
    wrapper_id: str,
    profile_id: str,
    wrapper_pid: int | None,
    claude_pid: int | None,
    cwd: str | None,
    now: datetime,
) -> dict[str, Any]:
    """A fresh `sessions/<wrapper_id>.json` document (ADR-0005)."""
    return {
        "schema": SESSION_SCHEMA,
        "wrapper_id": wrapper_id,
        "profile_id": profile_id,
        "wrapper_pid": wrapper_pid,
        "claude_pid": claude_pid,
        "session_id": None,
        "cwd": cwd,
        "started_at": format_iso(now),
        "model_id": None,
        "activity": "unknown",
        "supervision": {
            "state": "running",
            "holds": [],
            "paused_at": None,
            "resume_at": None,
            "was_busy_at_pause": False,
            "overridden_instances": [],
        },
    }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class Conn:
    """One client connection: an ordered outgoing queue plus pending command acks."""

    def __init__(
        self, daemon: Daemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.daemon = daemon
        self.reader = reader
        self.writer = writer
        self.client: str | None = None
        self.version: str | None = None
        self.topics: set[str] = set()
        self.wrapper_id: str | None = None
        self.closed = False
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._acks: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def push(self, obj: dict[str, Any]) -> None:
        """Queue one outgoing line (drops the connection when a client stops reading)."""
        if self.closed:
            return
        if self._queue.qsize() >= QUEUE_MAX:
            log.warning("client %s is not reading; closing it", self.client)
            self.close()
            return
        self._queue.put_nowait({"proto": PROTO, **obj})

    def close(self) -> None:
        """Stop after the queued lines are written."""
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)
        for fut in self._acks.values():
            if not fut.done():
                fut.set_result({"result": "not_connected", "detail": {}})

    async def writer_loop(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                data = (json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n").encode()
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            with contextlib.suppress(Exception):
                self.writer.close()

    def resolve_ack(self, msg: dict[str, Any]) -> None:
        cmd_id = msg.get("ack")
        fut = self._acks.get(cmd_id) if isinstance(cmd_id, str) else None
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def send_cmd(
        self,
        cmd_type: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = ACK_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Push `{"cmd": {cmd_id, type, …}}` and await the launcher's ack.

        Returns the ack (`{ack, result, detail}`); `result` is `timeout` or `not_connected`
        when no ack arrives.
        """
        cmd_id = uuid.uuid4().hex[:12]
        if self.closed:
            return {"ack": cmd_id, "result": "not_connected", "detail": {}}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._acks[cmd_id] = fut
        self.push({"cmd": {"cmd_id": cmd_id, "type": cmd_type, **(payload or {})}})
        try:
            ack = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            return {"ack": cmd_id, "result": "timeout", "detail": {}}
        finally:
            self._acks.pop(cmd_id, None)
        return {"ack": cmd_id, **{k: v for k, v in ack.items() if k != "proto"}}


class Daemon:
    """All daemon state plus the lifecycle (`start` / `stop` / `run`)."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        sock_path: Path | None = None,
        config_path: Path | None = None,
        extensions: Iterable[Callable[[Daemon], Any]] | None = None,
        enable_sampler: bool = True,
        enable_reaper: bool = True,
        tick_s: float = 1.0,
        live_scan_s: float = 1.0,
        config_scan_s: float = 2.0,
        reaper_s: float = 10.0,
    ) -> None:
        from ccs.daemon.poller import Poller
        from ccs.daemon.reaper import Reaper
        from ccs.daemon.sampler import Sampler
        from ccs.daemon.watchers import ConfigWatcher, LiveWatcher

        self.clock: Clock = clock or SystemClock()
        self.sock_path = Path(sock_path) if sock_path else paths.daemon_sock()
        self.config_path = Path(config_path) if config_path else paths.config_file()
        self.hooks = DaemonHooks()
        self.config: Config | None = None
        self.events = EventBus(clock=self.clock, config=lambda: self.config)
        self.polled: dict[str, UsageSnapshot] = {}
        self.snapshots: dict[str, UsageSnapshot] = {}
        self.live_reports: dict[str, LiveReport] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.wrapper_conns: dict[str, Conn] = {}
        self.conns: set[Conn] = set()
        self.other_sessions: dict[str, dict[str, int]] = {}
        self.started_at: datetime = self.clock.now()
        self.poller = Poller(self)
        self.live_watcher = LiveWatcher(self)
        self.config_watcher = ConfigWatcher(self)
        self.sampler = Sampler(self)
        self.reaper = Reaper(self)
        self._extensions = (
            list(extensions) if extensions is not None else ext_mod.resolve(ext_mod.EXTENSIONS)
        )
        self._enable_sampler = enable_sampler
        self._enable_reaper = enable_reaper
        self._tick_s = tick_s
        self._live_scan_s = live_scan_s
        self._config_scan_s = config_scan_s
        self._reaper_s = reaper_s
        self._lock: fsio.FileLock | None = None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._extra_tasks: list[Callable[[], Awaitable[Any]]] = []
        self._stop = asyncio.Event()
        self._snapshot_wanted = asyncio.Event()
        self._last_snapshot_write = 0.0
        self._conn_tasks: set[asyncio.Task[Any]] = set()
        self._ops: dict[str, Callable[[Conn, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "hello": self._op_hello,
            "status": self._op_status,
            "refresh": self._op_refresh,
            "subscribe": self._op_subscribe,
            "reload_config": self._op_reload_config,
            "register_wrapper": self._op_register_wrapper,
            "unregister_wrapper": self._op_unregister_wrapper,
            "wrapper_event": self._op_wrapper_event,
            "notify_test": self._op_notify_test,
        }

    # ------------------------------------------------------------ extension API

    def add_task(self, factory: Callable[[], Awaitable[Any]]) -> None:
        """Run `factory()` as a background task for the daemon's lifetime."""
        if self._server is not None:
            self._spawn(factory())
        else:
            self._extra_tasks.append(factory)

    def profile(self, profile_id: str) -> Profile | None:
        return self.config.profile(profile_id) if self.config else None

    def profile_ids(self) -> list[str]:
        return [p.id for p in self.config.profiles] if self.config else []

    def sessions_for(self, profile_id: str) -> list[dict[str, Any]]:
        """Session records of one profile (connected or not), oldest first."""
        recs = [r for r in self.sessions.values() if r.get("profile_id") == profile_id]
        return sorted(recs, key=lambda r: str(r.get("started_at") or ""))

    def write_session(self, wrapper_id: str) -> None:
        rec = self.sessions.get(wrapper_id)
        if rec is not None:
            fsio.atomic_write_json(paths.session_file(wrapper_id), rec, mode=0o644)

    def update_session(
        self, wrapper_id: str, mutate: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any] | None:
        """Mutate one record in place, write it, and refresh the snapshot."""
        rec = self.sessions.get(wrapper_id)
        if rec is None:
            return None
        mutate(rec)
        self.write_session(wrapper_id)
        self.request_snapshot_write()
        return rec

    async def send_cmd(
        self,
        wrapper_id: str,
        cmd_type: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = ACK_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send a command to a registered launcher (see `Conn.send_cmd`)."""
        conn = self.wrapper_conns.get(wrapper_id)
        if conn is None or conn.closed:
            return {"ack": None, "result": "not_connected", "detail": {}}
        return await conn.send_cmd(cmd_type, payload, timeout=timeout)

    def emit(self, event: Event) -> dict[str, Any] | None:
        return self.events.emit(event)

    def request_poll(
        self, profile_id: str, at: datetime | None = None
    ) -> asyncio.Future[UsageSnapshot | None]:
        return self.poller.request_poll(profile_id, at)

    def request_snapshot_write(self) -> None:
        self._snapshot_wanted.set()

    def supervisor_state(self, profile_id: str) -> dict[str, Any]:
        """Hook contributions for a profile's `supervisor` fields (P06/P08)."""
        return self.hooks.supervisor_fields(profile_id)

    # ------------------------------------------------------------ usage

    async def apply_usage(self, profile_id: str, *, force_write: bool = False) -> bool:
        """Re-merge polled data with live reports; write + notify when changed (or forced)."""
        now = self.clock.now()
        reports = [r for r in self.live_reports.values() if r.profile_id == profile_id]
        merged = merge(self.polled.get(profile_id), reports, now=now, profile_id=profile_id)
        if merged is None:
            return False
        merged = apply_staleness(merged, now)
        changed = self.snapshots.get(profile_id) != merged
        if changed or force_write:
            self.snapshots[profile_id] = merged
            try:
                write_snapshot(merged)
            except OSError as exc:
                log.warning("cannot write usage file for %s: %s", profile_id, exc)
            await self.hooks.fire_usage_updated(profile_id)
            self.request_snapshot_write()
        return changed

    # ------------------------------------------------------------ snapshot

    def build_snapshot(self) -> dict[str, Any] | None:
        if self.config is None:
            return None
        pids = self.profile_ids()
        sup: dict[str, dict[str, Any]] = {pid: self.supervisor_state(pid) for pid in pids}
        next_warmups: dict[str, datetime | None] = {}
        return build_widget_snapshot(
            self.config,
            {pid: self.snapshots.get(pid) for pid in pids},
            sup,
            {pid: self.sessions_for(pid) for pid in pids},
            self.other_sessions,
            next_warmups,
            self.clock.now(),
        )

    def write_snapshot_now(self) -> dict[str, Any] | None:
        doc = self.build_snapshot()
        if doc is None:
            return None
        try:
            write_widget_snapshot(doc)
        except OSError as exc:
            log.warning("cannot write widget snapshot: %s", exc)
        self._last_snapshot_write = time.monotonic()
        for conn in list(self.conns):
            if "snapshot" in conn.topics:
                conn.push({"snapshot": doc})
        return doc

    async def _snapshot_loop(self) -> None:
        while True:
            await self._snapshot_wanted.wait()
            wait = SNAPSHOT_MIN_INTERVAL_S - (time.monotonic() - self._last_snapshot_write)
            if wait > 0:
                await asyncio.sleep(wait)
            self._snapshot_wanted.clear()
            self.write_snapshot_now()

    # ------------------------------------------------------------ status

    def status_payload(self, profile_id: str | None = None) -> dict[str, Any]:
        now = self.clock.now()
        profiles: list[dict[str, Any]] = []
        for pid in self.profile_ids():
            if profile_id is not None and pid != profile_id:
                continue
            snap = self.snapshots.get(pid)
            contrib = self.supervisor_state(pid)
            supervisor = {"state": "normal", "holds": []}
            supervisor.update({k: v for k, v in contrib.items() if k != "next_warmup_at"})
            nxt = contrib.get("next_warmup_at")
            profiles.append(
                {
                    "id": pid,
                    "usage": snap.to_dict() if snap is not None else None,
                    "supervisor": supervisor,
                    "sessions": self.sessions_for(pid),
                    "other_sessions": self.other_sessions.get(
                        pid, {"interactive": 0, "background": 0}
                    ),
                    "next_warmup_at": format_iso(nxt) if isinstance(nxt, datetime) else nxt,
                }
            )
        return {
            "daemon": {
                "version": __version__,
                "pid": os.getpid(),
                "started_at": format_iso(self.started_at),
                "uptime_s": int((now - self.started_at).total_seconds()),
                "responsive": True,
                "app_connected": any(c.client == "app" and not c.closed for c in self.conns),
            },
            "profiles": profiles,
        }

    # ------------------------------------------------------------ config

    async def set_config(self, new: Config) -> None:
        old = self.config
        self.config = new
        self.poller.sync(old, new)
        await self.hooks.fire_config_changed(old, new)
        self.request_snapshot_write()

    # ------------------------------------------------------------ wrappers

    def _info(self, rec: dict[str, Any], *, overridden: bool, rereg: bool) -> WrapperInfo:
        return WrapperInfo(
            wrapper_id=str(rec.get("wrapper_id")),
            profile_id=str(rec.get("profile_id")),
            wrapper_pid=_int_or_none(rec.get("wrapper_pid")),
            claude_pid=_int_or_none(rec.get("claude_pid")),
            cwd=_str_or_none(rec.get("cwd")),
            started_overridden=overridden,
            reregistered=rereg,
            record=rec,
        )

    async def unregister(
        self, wrapper_id: str, *, exit_code: int | None, reason: str
    ) -> dict[str, Any] | None:
        """Forget a launcher: drop its record + file, emit `session.ended`, fire hooks."""
        rec = self.sessions.pop(wrapper_id, None)
        conn = self.wrapper_conns.pop(wrapper_id, None)
        if conn is not None:
            conn.wrapper_id = None
        with contextlib.suppress(FileNotFoundError):
            paths.session_file(wrapper_id).unlink()
        if rec is None:
            return None
        pid = str(rec.get("profile_id") or "") or None
        self.emit(
            Event(
                "session.ended",
                pid,
                None,
                {"wrapper_id": wrapper_id, "exit_code": exit_code, "reason": reason},
            )
        )
        await self.hooks.fire_wrapper_unregistered(self._info(rec, overridden=False, rereg=False))
        self.request_snapshot_write()
        return rec

    # ------------------------------------------------------------ ops

    async def _op_hello(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        conn.client = _str_or_none(msg.get("client")) or "unknown"
        conn.version = _str_or_none(msg.get("version"))
        return {
            "ok": True,
            "daemon": {
                "version": __version__,
                "pid": os.getpid(),
                "started_at": format_iso(self.started_at),
            },
        }

    async def _op_status(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        pid = _str_or_none(msg.get("profile_id"))
        if pid is not None and self.profile(pid) is None:
            return {"ok": False, "error": "unknown_profile"}
        return {"ok": True, **self.status_payload(pid)}

    async def _op_refresh(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        pid = _str_or_none(msg.get("profile_id"))
        if pid is not None and self.profile(pid) is None:
            return {"ok": False, "error": "unknown_profile"}
        for target in [pid] if pid else self.profile_ids():
            self.request_poll(target)
        return {"ok": True}

    async def _op_subscribe(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        topics = msg.get("topics")
        wanted = (
            {t for t in topics if t in ("events", "snapshot")}
            if isinstance(topics, list)
            else set()
        )
        if not wanted:
            return {"ok": False, "error": "bad_topics"}
        conn.topics |= wanted
        reply: dict[str, Any] = {"ok": True, "topics": sorted(conn.topics)}
        if "snapshot" in wanted:
            doc = self.build_snapshot()
            if doc is not None:
                conn.push({"snapshot": doc})
        return reply

    async def _op_notify_test(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        """Send one test notification (posted by the app, or the osascript fallback)."""
        record = self.emit(Event(TEST_TYPE, None, None, {}))
        app = any(c.client == "app" and not c.closed for c in self.conns)
        via = "app" if app else "osascript"
        return {"ok": record is not None, "app_connected": app, "via": via}

    async def _op_reload_config(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        result = await self.config_watcher.check_once(force=True)
        return {"ok": bool(result.get("ok")), **{k: v for k, v in result.items() if k != "ok"}}

    async def _op_register_wrapper(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        wrapper_id = _str_or_none(msg.get("wrapper_id"))
        profile_id = _str_or_none(msg.get("profile_id"))
        if wrapper_id is None or profile_id is None:
            return {"ok": False, "error": "bad_request", "detail": "wrapper_id and profile_id"}
        if self.profile(profile_id) is None:
            await self.config_watcher.check_once(force=True)
            if self.profile(profile_id) is None:
                return {"ok": False, "error": "unknown_profile"}
        wrapper_pid = _int_or_none(msg.get("wrapper_pid"))
        claude_pid = _int_or_none(msg.get("claude_pid"))
        cwd = _str_or_none(msg.get("cwd"))
        overridden = bool(msg.get("started_overridden"))
        rec = self.sessions.get(wrapper_id)
        rereg = rec is not None
        if rec is None:
            rec = new_session_record(
                wrapper_id=wrapper_id,
                profile_id=profile_id,
                wrapper_pid=wrapper_pid,
                claude_pid=claude_pid,
                cwd=cwd,
                now=self.clock.now(),
            )
            self.sessions[wrapper_id] = rec
        else:
            rec["profile_id"] = profile_id
            rec["wrapper_pid"] = wrapper_pid if wrapper_pid is not None else rec.get("wrapper_pid")
            rec["claude_pid"] = claude_pid if claude_pid is not None else rec.get("claude_pid")
            rec["cwd"] = cwd or rec.get("cwd")
        old = self.wrapper_conns.get(wrapper_id)
        if old is not None and old is not conn:
            old.wrapper_id = None
        self.wrapper_conns[wrapper_id] = conn
        conn.wrapper_id = wrapper_id
        conn.client = conn.client or "launcher"
        self.write_session(wrapper_id)
        if not rereg:
            self.emit(
                Event(
                    "session.started",
                    profile_id,
                    None,
                    {"wrapper_id": wrapper_id, "cwd": cwd, "claude_pid": claude_pid},
                )
            )
        extra = await self.hooks.fire_wrapper_registered(
            self._info(rec, overridden=overridden, rereg=rereg)
        )
        self.request_snapshot_write()
        sup = rec.get("supervision")
        holds = sup.get("holds", []) if isinstance(sup, dict) else []
        reply: dict[str, Any] = {"ok": True, "holds": holds, "supervision": sup}
        reply.update(extra)
        return reply

    async def _op_unregister_wrapper(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        wrapper_id = _str_or_none(msg.get("wrapper_id"))
        if wrapper_id is None:
            return {"ok": False, "error": "bad_request", "detail": "wrapper_id"}
        rec = await self.unregister(
            wrapper_id, exit_code=_int_or_none(msg.get("exit_code")), reason="exited"
        )
        return {"ok": True, "known": rec is not None}

    async def _op_wrapper_event(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        wrapper_id = _str_or_none(msg.get("wrapper_id")) or conn.wrapper_id
        rec = self.sessions.get(wrapper_id) if wrapper_id else None
        if rec is None:
            return {"ok": False, "error": "unknown_wrapper"}
        log.info("wrapper %s event %s %s", wrapper_id, msg.get("kind"), msg.get("detail"))
        await self.hooks.fire_wrapper_event(self._info(rec, overridden=False, rereg=True), msg)
        return {"ok": True}

    async def dispatch(self, conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op")
        if not isinstance(op, str):
            return {"ok": False, "error": "bad_request", "detail": "op"}
        handler = self._ops.get(op) or self.hooks.ops.get(op)
        if handler is None:
            if op in ("pause", "resume", "warmup"):
                return {"ok": False, "error": "not_implemented"}
            return {"ok": False, "error": "unknown_op"}
        try:
            return await handler(conn, msg)
        except Exception as exc:
            log.exception("op %s failed", op)
            return {"ok": False, "error": "internal_error", "detail": str(exc)}

    # ------------------------------------------------------------ connections

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = Conn(self, reader, writer)
        self.conns.add(conn)
        writer_task = asyncio.ensure_future(conn.writer_loop())
        greeted = False
        try:
            while not conn.closed:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    conn.push({"id": None, "ok": False, "error": "line_too_long"})
                    break
                except (ConnectionError, OSError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    conn.push({"id": None, "ok": False, "error": "bad_json"})
                    continue
                if not isinstance(msg, dict):
                    conn.push({"id": None, "ok": False, "error": "bad_json"})
                    continue
                if "ack" in msg:
                    conn.resolve_ack(msg)
                    continue
                req_id = msg.get("id")
                if msg.get("proto", PROTO) != PROTO:
                    conn.push({"id": req_id, "ok": False, "error": "proto_mismatch"})
                    break
                if not greeted:
                    if msg.get("op") != "hello":
                        conn.push({"id": req_id, "ok": False, "error": "hello_required"})
                        break
                    greeted = True
                reply = await self.dispatch(conn, msg)
                conn.push({"id": req_id, **reply})
        finally:
            conn.close()
            self.conns.discard(conn)
            if conn.wrapper_id and self.wrapper_conns.get(conn.wrapper_id) is conn:
                del self.wrapper_conns[conn.wrapper_id]  # record kept until reconnect/reap
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer_task, 2.0)

    def _on_event(self, record: dict[str, Any]) -> None:
        for conn in list(self.conns):
            if "events" in conn.topics:
                conn.push({"event": record})

    # ------------------------------------------------------------ lifecycle

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)

        def _done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception() is not None:
                log.error("daemon task failed", exc_info=t.exception())

        task.add_done_callback(_done)

    def _load_sessions(self) -> None:
        from ccs.daemon.reaper import pid_alive

        try:
            files = sorted(paths.sessions_dir().glob("*.json"))
        except OSError:
            return
        for path in files:
            rec = fsio.read_json(path)
            wrapper_id = rec.get("wrapper_id") if rec else None
            wpid = _int_or_none(rec.get("wrapper_pid")) if rec else None
            if (
                rec is None
                or not isinstance(wrapper_id, str)
                or wpid is None
                or not pid_alive(wpid)
            ):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                if rec is not None and isinstance(wrapper_id, str):
                    self.emit(
                        Event(
                            "session.ended",
                            _str_or_none(rec.get("profile_id")),
                            None,
                            {"wrapper_id": wrapper_id, "exit_code": None, "reason": "reaped"},
                        )
                    )
                continue
            self.sessions[wrapper_id] = rec

    def _load_config(self) -> Config | None:
        try:
            return store.load(self.config_path)[0]
        except store.ConfigMissing:
            try:
                return store.ensure_config(self.config_path)
            except store.ConfigError as exc:
                log.error("cannot seed config: %s", exc)
                return None
        except store.ConfigInvalid as exc:
            log.error("config invalid at startup: %s", exc)
            issues = [{"path": i.path, "message": i.message} for i in exc.issues]
            self.emit(Event("config.invalid", None, None, {"issues": issues}))
            return None
        except store.ConfigError as exc:
            log.error("cannot load config at startup: %s", exc)
            return None

    async def start(self) -> None:
        paths.ensure_state_layout()
        lock = fsio.try_lock(paths.daemon_lock())
        if lock is None:
            raise DaemonAlreadyRunning(str(paths.daemon_lock()))
        self._lock = lock
        with contextlib.suppress(FileNotFoundError):
            self.sock_path.unlink()
        self.started_at = self.clock.now()
        self.events.subscribe(self._on_event)
        cfg = self._load_config()
        self._load_sessions()
        if cfg is not None:
            for p in cfg.profiles:
                prev = read_snapshot(p.id)
                if prev is not None:
                    self.polled[p.id] = prev
                    self.snapshots[p.id] = prev
        old_umask = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=str(self.sock_path), limit=LINE_LIMIT
            )
        finally:
            os.umask(old_umask)
        for install in self._extensions:
            try:
                install(self)
            except Exception:
                log.exception("daemon extension install failed")
        self.config_watcher.mark_seen()
        if cfg is not None:
            await self.set_config(cfg)
        self.emit(Event("daemon.started", None, None, {"pid": os.getpid(), "version": __version__}))
        self._spawn(self._snapshot_loop())
        self._spawn(self._tick_loop())
        self._spawn(self.live_watcher.loop(self._live_scan_s))
        self._spawn(self.config_watcher.loop(self._config_scan_s))
        if self._enable_sampler:
            self._spawn(self.sampler.loop())
        if self._enable_reaper:
            self._spawn(self.reaper.loop(self._reaper_s))
        for factory in self._extra_tasks:
            self._spawn(factory())
        self.request_snapshot_write()
        log.info("daemon started (pid %s, socket %s)", os.getpid(), self.sock_path)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            await self.hooks.fire_tick(self.clock.now())

    async def stop(self) -> None:
        self.emit(Event("daemon.stopped", None, None, {"pid": os.getpid()}))
        await self.poller.shutdown()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
        for conn in list(self.conns):
            conn.close()
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.sock_path.unlink()
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        log.info("daemon stopped")

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Start, serve until SIGTERM/SIGINT, then stop gracefully."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop)
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()
