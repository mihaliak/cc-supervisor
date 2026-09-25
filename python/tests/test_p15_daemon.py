"""P15 daemon hardening (ADR-0022): socket input checks, crash resistance, launchd, symlinks."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import run_with_daemon, scenario, short_state_dir, wait_until, write_config

from ccs import fsio, paths
from ccs.clock import FakeClock
from ccs.config import store
from ccs.daemon import launchd
from ccs.daemon.client import AsyncDaemonClient, daemon_available
from ccs.daemon.reaper import MAX_PID, REREGISTER_GRACE_S, pid_alive
from ccs.daemon.server import QUEUE_MAX, Conn, Daemon
from ccs.events import Event, EventBus
from ccs.launcher.daemon_link import BACKOFF_S


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_state_dir(monkeypatch) as d:
        scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        paths.ensure_state_layout()
        yield d


def events() -> list[dict[str, Any]]:
    path = paths.events_file()
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text("utf-8").splitlines()]


async def connect(client: str = "cli") -> AsyncDaemonClient:
    c = AsyncDaemonClient()
    await c.connect()
    assert (await c.hello(client))["ok"] is True
    return c


def session_rec(wrapper_id: str, wrapper_pid: Any) -> dict[str, Any]:
    return {"schema": 1, "wrapper_id": wrapper_id, "profile_id": "work", "wrapper_pid": wrapper_pid}


# ---------------------------------------------------------------- 1. wrapper_id validation

BAD_IDS = ["../../newdir/pwned", "a/b", "..", "x" * 65, "ok\n", "", "café", 7, ["w1"]]


def test_paths_refuse_invalid_ids() -> None:
    assert paths.is_valid_id(str(uuid.uuid4()))  # the launcher's wrapper-id format
    assert paths.is_valid_id("w_1-A") and paths.is_valid_id("x" * 64)
    for bad in BAD_IDS:
        assert not paths.is_valid_id(bad)
    for bad_id in ("../x", "a/b", "ok\n", "x" * 65):
        with pytest.raises(ValueError):
            paths.session_file(bad_id)
        with pytest.raises(ValueError):
            paths.live_file(wrapper_id=bad_id)
        with pytest.raises(ValueError):
            paths.live_file(session_id=bad_id)


def test_socket_rejects_invalid_wrapper_ids(env: Path) -> None:
    victim = env / "victim.json"
    victim.write_text("{}")

    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        for bad in BAD_IDS:
            for op, extra in (
                ("register_wrapper", {"profile_id": "work", "wrapper_pid": os.getpid()}),
                ("unregister_wrapper", {}),
                ("wrapper_event", {"kind": "x"}),
                ("pause", {}),
            ):
                reply = await c.request(op, wrapper_id=bad, **extra)
                assert (reply["error"], reply["detail"]) == ("bad_request", "wrapper_id"), op
        # the traversal targets from the review: nothing written, nothing deleted
        await c.request("unregister_wrapper", wrapper_id="../victim")
        await c.close()
        assert daemon.sessions == {}

    run_with_daemon(body)
    assert victim.exists()
    assert not (env.parent / "newdir" / "pwned.json").exists()
    assert sorted(p.name for p in paths.sessions_dir().iterdir()) == []


def test_unregister_unknown_wrapper_keeps_file(env: Path) -> None:
    orphan = paths.session_file("orphan")

    async def body(daemon: Daemon) -> None:
        fsio.atomic_write_json(orphan, {"not": "ours"})  # written after startup loading
        c = await connect("launcher")
        reply = await c.request("unregister_wrapper", wrapper_id="orphan")
        assert reply["ok"] is True and reply["known"] is False
        await c.close()

    run_with_daemon(body)
    assert orphan.exists()


# ---------------------------------------------------------------- 2. pids


def test_pid_alive_treats_impossible_pids_as_dead() -> None:
    assert not pid_alive(2**40)
    assert not pid_alive(MAX_PID)
    assert pid_alive(os.getpid())


def test_register_rejects_out_of_range_pids(env: Path) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        for key in ("wrapper_pid", "claude_pid"):
            for bad in (2**40, 2**31, 0, -1, True, "123", 1.5):
                fields = {"wrapper_pid": os.getpid(), key: bad}
                reply = await c.request(
                    "register_wrapper", wrapper_id="w1", profile_id="work", **fields
                )
                assert reply["error"] == "bad_request" and reply["detail"] == key, (key, bad)
        assert daemon.sessions == {}
        ok = await c.request(
            "register_wrapper", wrapper_id="w1", profile_id="work", wrapper_pid=MAX_PID
        )
        assert ok["ok"] is True
        await c.close()

    run_with_daemon(body)


def test_reaper_survives_bad_records(env: Path) -> None:
    async def body(daemon: Daemon) -> None:
        daemon.sessions["broken"] = "not a record"  # type: ignore[assignment]
        daemon.sessions["huge"] = session_rec("huge", 2**40)
        daemon.sessions["alive"] = session_rec("alive", os.getpid())
        daemon.sessions["gone"] = session_rec("gone", MAX_PID)
        reaped = await daemon.reaper.reap_once()
        assert reaped == ["huge", "gone"]
        assert set(daemon.sessions) == {"broken", "alive"}

    run_with_daemon(body)


def test_startup_survives_bad_session_files(env: Path) -> None:
    sessions = paths.sessions_dir()
    fsio.atomic_write_json(sessions / "huge.json", session_rec("huge", 2**40))
    fsio.atomic_write_json(sessions / "flag.json", session_rec("flag", True))
    fsio.atomic_write_json(sessions / "renamed.json", session_rec("other", os.getpid()))
    fsio.atomic_write_json(sessions / "bad.json", session_rec("../../x", os.getpid()))
    (sessions / "bytes.json").write_bytes(b'{"wrapper_id": "\xff"}')
    (sessions / "dir.json").mkdir()
    fsio.atomic_write_json(sessions / "here.json", session_rec("here", os.getpid()))

    async def body(daemon: Daemon) -> None:
        assert set(daemon.sessions) == {"here"}

    run_with_daemon(body)
    assert sorted(p.name for p in sessions.iterdir()) == ["dir.json", "here.json"]


def test_startup_survives_unexpected_session_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccs.daemon import reaper

    real = reaper.pid_alive

    def flaky(pid: int) -> bool:
        if pid == 4242:
            raise RuntimeError("boom")
        return real(pid)

    monkeypatch.setattr(reaper, "pid_alive", flaky)
    fsio.atomic_write_json(paths.session_file("boom"), session_rec("boom", 4242))
    fsio.atomic_write_json(paths.session_file("here"), session_rec("here", os.getpid()))

    async def body(daemon: Daemon) -> None:
        assert set(daemon.sessions) == {"here"}

    run_with_daemon(body)
    assert not paths.session_file("boom").exists()


# ---------------------------------------------------------------- 3. config read errors


def test_config_read_errors_are_config_invalid(tmp_path: Path) -> None:
    bad_utf8 = tmp_path / "utf8.json"
    bad_utf8.write_bytes(b'{"name": "Caf\xe9"}')
    a_dir = tmp_path / "dir.json"
    a_dir.mkdir()
    for path in (bad_utf8, a_dir):
        with pytest.raises(store.ConfigInvalid) as info:
            store.load_raw(path)
        assert info.value.issues[0].path == ""
    if os.geteuid() != 0:
        locked = tmp_path / "locked.json"
        locked.write_text("{}")
        locked.chmod(0)
        with pytest.raises(store.ConfigInvalid, match="cannot read config"):
            store.load_raw(locked)
    with pytest.raises(store.ConfigMissing):
        store.load_raw(tmp_path / "missing.json")


def test_daemon_starts_and_reloads_with_undecodable_config(env: Path) -> None:
    cfg = paths.config_file()
    good = cfg.read_bytes()
    cfg.write_bytes(good.replace(b'"Work"', b'"Caf\xe9"'))

    async def body(daemon: Daemon) -> None:
        assert daemon.config is None
        c = await connect()
        reply = await c.request("reload_config")
        assert reply["ok"] is False and "UTF-8" in reply["issues"][0]["message"]
        cfg.write_bytes(good)
        assert (await c.request("reload_config"))["ok"] is True
        assert daemon.profile("work") is not None
        await c.close()

    run_with_daemon(body, config_scan_s=3600)
    assert "config.invalid" in [e["type"] for e in events()]


# ---------------------------------------------------------------- 4. encoding


def test_surrogates_from_socket_are_replaced(env: Path) -> None:
    async def body(daemon: Daemon) -> None:
        c = await connect("launcher")
        reply = await c.request(
            "register_wrapper",
            wrapper_id="w1",
            profile_id="work",
            wrapper_pid=os.getpid(),
            cwd="/tmp/\udcff",
        )
        assert reply["ok"] is True
        assert (await c.request("status"))["ok"] is True
        await c.close()
        assert daemon.sessions["w1"]["cwd"] == "/tmp/�"

    run_with_daemon(body)
    on_disk = json.loads(paths.session_file("w1").read_text("utf-8"))
    assert on_disk["cwd"] == "/tmp/�"
    started = [e for e in events() if e["type"] == "session.started"]
    assert started[0]["data"]["cwd"] == "/tmp/�"


def test_writer_survives_unencodable_items(env: Path) -> None:
    async def boom(conn: Conn, msg: dict[str, Any]) -> dict[str, Any]:
        conn.push({"event": {"bad": object()}})  # an unencodable push is dropped
        return {"ok": True, "bad": "\udcff"}  # an unencodable reply becomes internal_error

    async def body(daemon: Daemon) -> None:
        daemon.hooks.handle_op("boom", boom)
        c = await connect()
        reply = await c.request("boom")
        assert reply == {"proto": 1, "id": reply["id"], "ok": False, "error": "internal_error"}
        assert (await c.request("status"))["ok"] is True  # same connection still works
        await c.close()

    run_with_daemon(body)


def test_snapshot_loop_survives_a_failure(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(daemon: Daemon) -> None:
        await wait_until(lambda: paths.widget_snapshot().exists())
        paths.widget_snapshot().unlink()
        real = daemon.write_snapshot_now
        calls: list[int] = []

        def flaky() -> dict[str, Any] | None:
            calls.append(1)
            if len(calls) == 1:
                raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")
            return real()

        monkeypatch.setattr(daemon, "write_snapshot_now", flaky)
        daemon.request_snapshot_write()
        await wait_until(lambda: len(calls) >= 1)
        daemon.request_snapshot_write()
        await wait_until(lambda: paths.widget_snapshot().exists())

    run_with_daemon(body)


def test_dedupe_key_remembered_only_after_append(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.mkdir()  # appends fail
    bus = EventBus(clock=FakeClock(), events_path=events_path, seen_path=tmp_path / "seen.json")
    pushed: list[dict[str, Any]] = []
    bus.subscribe(pushed.append)
    assert bus.emit(Event("limit.warn", "work", "k1", {})) is not None
    assert not bus.seen("k1", bus.clock.now())
    events_path.rmdir()
    assert bus.emit(Event("limit.warn", "work", "k1", {})) is not None  # not suppressed
    assert bus.seen("k1", bus.clock.now())
    assert bus.emit(Event("limit.warn", "work", "k1", {})) is None
    # data that cannot be encoded: logged as a failed append, never raised
    assert bus.emit(Event("limit.warn", "work", "k2", {"x": object()})) is not None
    assert not bus.seen("k2", bus.clock.now())
    # lone surrogates are replaced, so the record is logged
    assert bus.emit(Event("session.started", "work", "k3", {"cwd": "/\udcff"})) is not None
    assert bus.seen("k3", bus.clock.now())
    logged = [json.loads(x) for x in events_path.read_text("utf-8").splitlines()]
    assert [e["key"] for e in logged] == ["k1", "k3"]
    assert logged[-1]["data"]["cwd"] == "/�"
    assert len(pushed) == 4  # subscribers get every non-deduped event, logged or not


# ---------------------------------------------------------------- 5. stalled clients


def test_stalled_client_is_aborted(env: Path) -> None:
    async def body(daemon: Daemon) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        await loop.sock_connect(sock, str(daemon.sock_path))
        await loop.sock_sendall(sock, b'{"proto":1,"id":1,"op":"hello","client":"app"}\n')
        await loop.sock_sendall(sock, b'{"proto":1,"id":2,"op":"subscribe","topics":["events"]}\n')
        await wait_until(lambda: any("events" in c.topics for c in daemon.conns))
        for i in range(QUEUE_MAX + 10):  # more than the queue holds, before any write
            daemon.emit(Event("session.started", "work", None, {"n": i, "pad": "x" * 500}))
        await wait_until(lambda: not daemon.conns)
        received = b""
        try:
            while chunk := await asyncio.wait_for(loop.sock_recv(sock, 65536), 5):
                received += chunk
        except ConnectionResetError:
            pass
        sock.close()
        assert len(received) < 1024 * 1024  # EOF came instead of the backlog

    run_with_daemon(body)


# ---------------------------------------------------------------- 6. unknown profile reload


def test_status_and_refresh_reload_config_for_new_profile(env: Path, tmp_path: Path) -> None:
    new_dir = tmp_path / "profile-new"
    new_dir.mkdir()

    def add_profile(raw: dict[str, Any]) -> None:
        raw["profiles"].append(
            {"id": "new", "flag": "new", "name": "New", "emoji": "N", "config_dir": str(new_dir)}
        )

    async def body(daemon: Daemon) -> None:
        c = await connect()
        assert (await c.request("status", profile_id="new"))["error"] == "unknown_profile"
        store.save(add_profile)  # the watcher is too slow to notice in this test
        status = await c.request("status", profile_id="new")
        assert status["ok"] is True and [p["id"] for p in status["profiles"]] == ["new"]
        assert (await c.request("refresh", profile_id="new"))["ok"] is True
        assert (await c.request("refresh", profile_id="nope"))["error"] == "unknown_profile"
        await c.close()

    run_with_daemon(body, config_scan_s=3600)


# ---------------------------------------------------------------- 8. launchd lock wait


def test_launched_by_agent_detection() -> None:
    assert launchd.launched_by_agent({"XPC_SERVICE_NAME": launchd.LABEL})
    assert not launchd.launched_by_agent({"XPC_SERVICE_NAME": "0"})
    assert not launchd.launched_by_agent({})


def test_run_waits_for_lock_then_starts(env: Path) -> None:
    async def main() -> None:
        held = fsio.try_lock(paths.daemon_lock())
        assert held is not None
        daemon = Daemon(extensions=[], enable_sampler=False, enable_reaper=False)
        task = asyncio.ensure_future(daemon.run(wait_for_lock=True))
        await asyncio.sleep(0.3)
        assert not task.done() and not paths.daemon_sock().exists()
        held.release()
        await wait_until(lambda: paths.daemon_sock().exists())
        daemon.request_stop()
        await asyncio.wait_for(task, 20)

    asyncio.run(main())
    assert [e["type"] for e in events()] == ["daemon.started", "daemon.stopped"]


def test_stop_while_waiting_for_lock(env: Path) -> None:
    async def main() -> None:
        held = fsio.try_lock(paths.daemon_lock())
        assert held is not None
        daemon = Daemon(extensions=[], enable_sampler=False, enable_reaper=False)
        task = asyncio.ensure_future(daemon.run(wait_for_lock=True))
        await asyncio.sleep(0.2)
        daemon.request_stop()
        await asyncio.wait_for(task, 5)
        held.release()

    asyncio.run(main())
    assert events() == []  # never started


def _ccs(*args: str) -> list[str]:
    return [sys.executable, "-m", "ccs", *args]


def _child_env(**extra: str) -> dict[str, str]:
    child = {k: v for k, v in os.environ.items() if k != "XPC_SERVICE_NAME"}
    child.update(extra)
    return child


def test_launchd_run_waits_for_lock_and_stops_on_sigterm(env: Path) -> None:
    held = fsio.try_lock(paths.daemon_lock())
    assert held is not None
    proc = subprocess.Popen(
        _ccs("daemon", "run"),
        env=_child_env(XPC_SERVICE_NAME=launchd.LABEL),  # a plist written before the flag
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(2.0)
        assert proc.poll() is None  # waiting instead of exiting (launchd would respawn it)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        held.release()
    assert proc.returncode == 0 and "already running" not in out
    assert "waiting for it to exit" in paths.daemon_log().read_text()


def test_launchd_flag_run_takes_over_when_lock_frees(env: Path) -> None:
    held = fsio.try_lock(paths.daemon_lock())
    assert held is not None
    proc = subprocess.Popen(
        _ccs("daemon", "run", launchd.LAUNCHD_FLAG),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.5)
        assert proc.poll() is None and not daemon_available()
        held.release()
        deadline = time.monotonic() + 15
        while not daemon_available() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert daemon_available()
    finally:
        held.release()
        proc.send_signal(signal.SIGTERM)
        proc.wait(20)
    assert proc.returncode == 0


# ---------------------------------------------------------------- 9/10. plist env + executable


def test_install_env_passes_ccs_state_dir() -> None:
    env = launchd.install_env({"PATH": "/p", "HOME": "/h", "CCS_STATE_DIR": "/s", "X": "y"})
    assert env == {"PATH": "/p", "HOME": "/h", "CCS_STATE_DIR": "/s"}


def test_ccs_executable_prefers_running_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = tmp_path / "venv" / "bin" / "ccs"
    running.parent.mkdir(parents=True)
    running.write_text("#!/bin/sh\n")
    running.chmod(0o755)
    monkeypatch.setattr(launchd.shutil, "which", lambda name: "/elsewhere/bin/ccs")
    assert launchd.ccs_executable(str(running)) == str(running)
    monkeypatch.chdir(running.parent)
    assert launchd.ccs_executable("./ccs") == str(running)
    # `python -m ccs`, or a `ccs` that is not an executable: fall back to PATH
    assert launchd.ccs_executable("/x/ccs/__main__.py") == "/elsewhere/bin/ccs"
    running.chmod(0o644)
    assert launchd.ccs_executable(str(running)) == "/elsewhere/bin/ccs"
    monkeypatch.setattr(launchd.shutil, "which", lambda name: None)
    assert launchd.ccs_executable("/x/ccs/__main__.py") == "/x/ccs/__main__.py"


# ---------------------------------------------------------------- 11. provisional sessions


def test_grace_covers_launcher_reconnect_backoff() -> None:
    assert max(60.0, 3 * max(BACKOFF_S)) <= REREGISTER_GRACE_S


def test_loaded_sessions_must_reregister_within_grace(env: Path) -> None:
    clock = FakeClock()
    for wid in ("back", "phantom"):
        fsio.atomic_write_json(paths.session_file(wid), session_rec(wid, os.getpid()))

    async def body(daemon: Daemon) -> None:
        assert set(daemon.provisional) == {"back", "phantom"}
        assert await daemon.reaper.reap_once() == []
        c = await connect("launcher")
        reply = await c.request(
            "register_wrapper", wrapper_id="back", profile_id="work", wrapper_pid=os.getpid()
        )
        assert reply["ok"] is True
        clock.advance(REREGISTER_GRACE_S - 1)
        assert await daemon.reaper.reap_once() == []
        clock.advance(1)
        assert await daemon.reaper.reap_once() == ["phantom"]  # its pid is alive: reused
        assert set(daemon.sessions) == {"back"} and daemon.provisional == {}
        await c.close()

    run_with_daemon(body, clock=clock)
    assert not paths.session_file("phantom").exists()
    ended = [e["data"] for e in events() if e["type"] == "session.ended"]
    assert [(d["wrapper_id"], d["reason"]) for d in ended] == [("phantom", "stale")]


# ---------------------------------------------------------------- 12. symlinks


def test_atomic_write_follows_symlinked_settings(tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "claude-settings.json"
    target.write_text('{"old": true}')
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    link = config_dir / "settings.json"
    link.symlink_to(Path("..") / "dotfiles" / "claude-settings.json")  # relative, like stow
    fsio.atomic_write_json(link, {"statusLine": {"type": "command"}}, mode=0o644)
    assert link.is_symlink() and os.readlink(link) == "../dotfiles/claude-settings.json"
    assert json.loads(target.read_text()) == {"statusLine": {"type": "command"}}
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert sorted(p.name for p in dotfiles.iterdir()) == ["claude-settings.json"]
    assert sorted(p.name for p in config_dir.iterdir()) == ["settings.json"]
    dangling = config_dir / "new.json"
    dangling.symlink_to(dotfiles / "new.json")
    fsio.atomic_write_json(dangling, {"v": 1})
    assert dangling.is_symlink() and json.loads((dotfiles / "new.json").read_text()) == {"v": 1}


def test_config_save_keeps_symlinked_config(tmp_path: Path, tmp_xdg: object) -> None:
    write_config(tmp_path)
    cfg = paths.config_file()
    real = tmp_path / "dotfiles" / "ccs-config.json"
    real.parent.mkdir()
    cfg.rename(real)
    cfg.symlink_to(real)
    store.save(lambda raw: raw.update({"default_profile": "work"}))
    assert cfg.is_symlink()
    assert json.loads(real.read_text())["revision"] == 1
