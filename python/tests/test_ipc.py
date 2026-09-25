"""Socket protocol: handshake, ops, subscribe pushes, wrapper registry, command acks, hooks."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import (
    calls,
    make_daemon,
    run_with_daemon,
    scenario,
    short_state_dir,
    wait_until,
    write_config,
)

from ccs import fsio, paths
from ccs.daemon.client import AsyncDaemonClient
from ccs.daemon.hooks import WrapperInfo
from ccs.daemon.server import Conn, Daemon
from ccs.events import Event


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        log = scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        yield {"state": d, "log": log}


async def connect(client: str = "cli") -> AsyncDaemonClient:
    c = AsyncDaemonClient()
    await c.connect()
    reply = await c.hello(client)
    assert reply["ok"] is True
    return c


def test_hello_reply(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        c = AsyncDaemonClient()
        await c.connect()
        reply = await c.hello()
        assert reply["daemon"]["pid"] == os.getpid()
        assert reply["proto"] == 1
        await c.close()

    run_with_daemon(body)


def test_proto_mismatch_closes(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        reader, writer = await asyncio.open_unix_connection(str(paths.daemon_sock()))
        writer.write(b'{"proto": 2, "id": 1, "op": "hello"}\n')
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert reply == {"proto": 1, "id": 1, "ok": False, "error": "proto_mismatch"}
        assert await reader.readline() == b""  # closed
        writer.close()

    run_with_daemon(body)


def test_hello_required_first(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        reader, writer = await asyncio.open_unix_connection(str(paths.daemon_sock()))
        writer.write(b'{"proto": 1, "id": 7, "op": "status"}\n')
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert reply["error"] == "hello_required" and reply["id"] == 7
        writer.close()

    run_with_daemon(body)


def test_bad_json_and_unknown_op(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect()
        assert (await c.request("frobnicate"))["error"] == "unknown_op"
        for op in ("pause", "resume", "warmup"):
            assert (await c.request(op))["error"] == "not_implemented"
        await c.close()

    run_with_daemon(body)


def test_line_too_long_closes(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        reader, writer = await asyncio.open_unix_connection(
            str(paths.daemon_sock()), limit=4 * 1024 * 1024
        )
        writer.write(b'{"proto":1,"id":1,"op":"hello"}\n')
        writer.write(b"x" * (1024 * 1024 + 10) + b"\n")
        await writer.drain()
        first = json.loads(await reader.readline())
        assert first["ok"] is True
        second = json.loads(await reader.readline())
        assert second["error"] == "line_too_long"
        writer.close()

    run_with_daemon(body)


def test_status_shape(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: daemon.snapshots.get("work") is not None)
        c = await connect()
        reply = await c.request("status")
        assert reply["ok"] is True
        assert set(reply["daemon"]) >= {"version", "pid", "started_at", "uptime_s"}
        (profile,) = reply["profiles"]
        assert profile["id"] == "work"
        assert profile["usage"]["status"] == "ok"
        assert profile["supervisor"] == {"state": "normal", "holds": []}
        assert profile["sessions"] == []
        assert profile["other_sessions"] == {"interactive": 0, "background": 0}
        assert profile["next_warmup_at"] is None
        assert (await c.request("status", profile_id="nope"))["error"] == "unknown_profile"
        await c.close()

    run_with_daemon(body)


def test_refresh_triggers_exactly_one_probe(env: dict[str, Any]) -> None:
    log: Path = env["log"]

    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: len(calls(log, "stream")) == 1)
        await wait_until(lambda: daemon.snapshots.get("work") is not None)
        c = await connect()
        assert (await c.request("refresh", profile_id="work"))["ok"] is True
        await wait_until(lambda: len(calls(log, "stream")) == 2)
        await asyncio.sleep(0.5)
        await c.close()

    run_with_daemon(body)
    assert len(calls(log, "stream")) == 2


def test_subscribe_receives_events_and_snapshots(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect("app")
        reply = await c.request("subscribe", topics=["events", "snapshot"])
        assert reply["ok"] is True and reply["topics"] == ["events", "snapshot"]
        first = await c.next_push(5)
        assert first is not None and "snapshot" in first  # initial snapshot on subscribe
        daemon.emit(Event("session.started", "work", None, {"x": 1}))
        seen_event = seen_snapshot = False
        for _ in range(20):
            push = await c.next_push(5)
            assert push is not None
            if "event" in push and push["event"]["type"] == "session.started":
                assert push["event"]["data"]["title"]
                seen_event = True
            if "snapshot" in push:
                seen_snapshot = True
            daemon.request_snapshot_write()
            if seen_event and seen_snapshot:
                break
        assert seen_event and seen_snapshot
        assert (await c.request("subscribe", topics=["nope"]))["error"] == "bad_topics"
        await c.close()

    run_with_daemon(body)


def test_register_and_unregister_wrapper(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        reply = await c.request(
            "register_wrapper",
            wrapper_id="w1",
            profile_id="work",
            wrapper_pid=os.getpid(),
            claude_pid=12345,
            cwd="/tmp/x",
        )
        assert reply["ok"] is True and reply["holds"] == []
        assert reply["supervision"]["state"] == "running"
        rec = json.loads(paths.session_file("w1").read_text())
        assert rec["profile_id"] == "work" and rec["claude_pid"] == 12345
        assert rec["activity"] == "unknown" and rec["schema"] == 1
        bad = await c.request("register_wrapper", wrapper_id="w2", profile_id="nope")
        assert bad["error"] == "unknown_profile"
        # re-register (reconnect) keeps the record and emits no second session.started
        c2 = await connect("launcher")
        await c2.request(
            "register_wrapper", wrapper_id="w1", profile_id="work", wrapper_pid=os.getpid()
        )
        assert daemon.wrapper_conns["w1"] is not None
        gone = await c2.request("unregister_wrapper", wrapper_id="w1", exit_code=0)
        assert gone == {"proto": 1, "id": gone["id"], "ok": True, "known": True}
        assert not paths.session_file("w1").exists()
        await c.close()
        await c2.close()

    run_with_daemon(body)
    events = [json.loads(x) for x in paths.events_file().read_text().splitlines()]
    types = [e["type"] for e in events if e["type"].startswith("session.")]
    assert types == ["session.started", "session.ended"]
    ended = next(e for e in events if e["type"] == "session.ended")
    assert ended["data"]["reason"] == "exited" and ended["data"]["exit_code"] == 0


def test_disconnect_keeps_record(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        await c.request("register_wrapper", wrapper_id="w1", profile_id="work", wrapper_pid=1)
        await c.close()
        await wait_until(lambda: "w1" not in daemon.wrapper_conns)
        assert "w1" in daemon.sessions and paths.session_file("w1").exists()

    run_with_daemon(body)


def test_send_cmd_ack_and_timeout(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        await c.request("register_wrapper", wrapper_id="w1", profile_id="work", wrapper_pid=1)

        async def launcher() -> None:
            while True:
                push = await c.next_push(5)
                assert push is not None
                if "cmd" in push:
                    cmd = push["cmd"]
                    await c.send({"ack": cmd["cmd_id"], "result": "injected", "detail": {"x": 1}})
                    return

        task = asyncio.ensure_future(launcher())
        ack = await daemon.send_cmd("w1", "pause", {"holds": ["session"]})
        await task
        assert ack["result"] == "injected" and ack["detail"] == {"x": 1}
        timed_out = await daemon.send_cmd("w1", "resume", {"prompt": None}, timeout=0.2)
        assert timed_out["result"] == "timeout"
        missing = await daemon.send_cmd("nobody", "pause")
        assert missing["result"] == "not_connected"
        await c.close()

    run_with_daemon(body)


def test_hooks_ops_and_register_reply(env: dict[str, Any]) -> None:
    seen: list[str] = []

    def install(daemon: Daemon) -> None:
        async def pause(conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "paused": msg.get("profile_id")}

        def registered(info: WrapperInfo) -> dict[str, Any]:
            seen.append(f"reg:{info.wrapper_id}:{info.started_overridden}")
            return {"holds": ["manual"], "extra": 1}

        def event(info: WrapperInfo, msg: dict[str, Any]) -> None:
            seen.append(f"evt:{msg.get('kind')}")

        daemon.hooks.handle_op("pause", pause)
        daemon.hooks.on_wrapper_registered(registered)
        daemon.hooks.on_wrapper_event(event)
        daemon.hooks.contribute_supervisor(lambda pid: {"state": "warned"})

    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        assert (await c.request("pause", profile_id="work"))["paused"] == "work"
        reply = await c.request(
            "register_wrapper",
            wrapper_id="w9",
            profile_id="work",
            wrapper_pid=1,
            started_overridden=True,
        )
        assert reply["holds"] == ["manual"] and reply["extra"] == 1
        ev = await c.request("wrapper_event", wrapper_id="w9", kind="injected", detail={})
        assert ev["ok"] is True
        status = await c.request("status")
        assert status["profiles"][0]["supervisor"]["state"] == "warned"
        await c.close()

    run_with_daemon(body, extensions=[install])
    assert seen == ["reg:w9:True", "evt:injected"]


def test_reload_config_reports_issues(env: dict[str, Any], tmp_path: Path) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect()
        ok = await c.request("reload_config")
        assert ok["ok"] is True
        raw = json.loads(paths.config_file().read_text())
        raw["profiles"][0]["limits"] = {"session": {"warn": 95, "pause": 90}}
        paths.config_file().write_text(json.dumps(raw))
        bad = await c.request("reload_config")
        assert bad["ok"] is False and bad["issues"]
        assert daemon.config is not None and daemon.config.profile("work") is not None
        await c.close()

    run_with_daemon(body)


def test_second_daemon_refuses_lock(env: dict[str, Any]) -> None:
    from ccs.daemon.server import DaemonAlreadyRunning

    async def body(daemon: Daemon) -> None:
        other = Daemon(extensions=[], enable_sampler=False, enable_reaper=False)
        with pytest.raises(DaemonAlreadyRunning):
            await other.start()

    run_with_daemon(body)


def _listening_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen()
    return sock


def _lock_is_held() -> bool:
    probe = fsio.try_lock(paths.daemon_lock())
    if probe is None:
        return True
    probe.release()
    return False


def test_deleted_lock_does_not_allow_a_second_daemon(env: dict[str, Any]) -> None:
    """A second daemon never steals the socket of one whose lock file was deleted."""
    from ccs.daemon.server import DaemonAlreadyRunning

    async def body(daemon: Daemon) -> None:
        paths.daemon_lock().unlink()
        other = Daemon(extensions=[], enable_sampler=False, enable_reaper=False)
        try:
            with pytest.raises(DaemonAlreadyRunning):
                await other.start()
        finally:
            if other._server is not None:
                await other.stop()
        c = await connect()  # the first daemon still serves its socket
        assert (await c.request("status"))["ok"] is True
        await c.close()

    run_with_daemon(body)


def test_daemon_retakes_a_deleted_lock(env: dict[str, Any]) -> None:
    async def body(daemon: Daemon) -> None:
        paths.daemon_lock().unlink()
        await wait_until(lambda: paths.daemon_lock().exists() and _lock_is_held())
        assert daemon._lock is not None and daemon._lock.is_current()

    run_with_daemon(body, tick_s=0.05)


def test_daemon_stops_when_another_took_lock_and_socket(env: dict[str, Any]) -> None:
    async def main() -> None:
        daemon = make_daemon(tick_s=0.05)
        await daemon.start()
        thief = None
        other_sock = None
        try:
            paths.daemon_lock().unlink()
            thief = fsio.try_lock(paths.daemon_lock())  # a newer daemon's fresh lock file
            paths.daemon_sock().unlink()
            other_sock = _listening_socket(paths.daemon_sock())
            await asyncio.wait_for(daemon._stop.wait(), 5)
        finally:
            await daemon.stop()
            assert paths.daemon_sock().exists()  # the newer daemon's socket is kept
            if other_sock is not None:
                other_sock.close()
            if thief is not None:
                thief.release()

    asyncio.run(main())


def test_stop_keeps_a_socket_that_is_not_ours(env: dict[str, Any]) -> None:
    held: list[socket.socket] = []

    async def body(daemon: Daemon) -> None:
        paths.daemon_sock().unlink()
        held.append(_listening_socket(paths.daemon_sock()))

    try:
        run_with_daemon(body)
        assert paths.daemon_sock().exists()
    finally:
        for sock in held:
            sock.close()


def test_notify_test_routes_by_events_subscription(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app that hasn't subscribed to `events` never gets the event: the reported route
    and the osascript fallback both treat it as absent."""
    from ccs import notify

    posted: list[list[str]] = []

    async def fake_osascript(argv: list[str]) -> int:
        posted.append(argv)
        return 0

    monkeypatch.setattr(notify, "run_osascript", fake_osascript)

    async def body(daemon: Daemon) -> None:
        app = await connect("app")  # connected, not listening for events yet
        reply = await app.request("notify_test")
        assert reply["ok"] is True
        assert (reply["via"], reply["app_connected"]) == ("osascript", False)
        await wait_until(lambda: len(posted) == 1)
        status = await app.request("status")
        assert status["daemon"]["app_connected"] is False
        await app.request("subscribe", topics=["events"])
        reply = await app.request("notify_test")
        assert (reply["via"], reply["app_connected"]) == ("app", True)
        push = await app.next_push(5)
        assert push is not None and push["event"]["type"] == "notify.test"
        await asyncio.sleep(0.1)
        assert len(posted) == 1  # posted by the app, not the fallback
        assert (await app.request("status"))["daemon"]["app_connected"] is True
        await app.close()

    run_with_daemon(body, extensions=[notify.install])
