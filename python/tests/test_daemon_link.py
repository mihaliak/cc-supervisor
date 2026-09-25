"""Launcher ↔ daemon link (P05): unsupervised hints, autostart, register, reconnect, commands."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import (
    make_daemon,
    run_with_daemon,
    scenario,
    short_state_dir,
    wait_until,
    write_config,
)

from ccs import paths
from ccs.config import store
from ccs.daemon.server import Daemon
from ccs.launcher.daemon_link import DaemonLink, Registration

FAST = {"connect_timeout": 0.5, "start_retries_s": (0.05, 0.1, 0.2), "backoff_s": (0.05, 0.1)}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        yield d


class FakeLaunchd:
    def __init__(self, installed: bool, on_start: Any = None) -> None:
        self.installed = installed
        self.on_start = on_start
        self.starts = 0

    def is_installed(self) -> bool:
        return self.installed

    def start(self) -> None:
        self.starts += 1
        if self.on_start is not None:
            self.on_start()


def reg(wrapper_id: str = "w-1", overridden: bool = False) -> Registration:
    return Registration(
        wrapper_id=wrapper_id,
        profile_id="work",
        wrapper_pid=111,
        claude_pid=222,
        cwd="/tmp",
        started_overridden=overridden,
    )


async def no_cmd(cmd: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "skipped", {}


def test_not_installed_runs_unsupervised(env: Path) -> None:
    printed: list[str] = []

    async def body() -> None:
        link = DaemonLink("work", "w-1", launchd=FakeLaunchd(False), err=printed.append, **FAST)
        assert await link.connect() is False
        assert link.state == "unsupervised"
        link.start(reg(), no_cmd)
        await link.close(0)

    asyncio.run(body())
    assert printed == ["ccs: supervisor not installed — running unsupervised (ccs daemon install)"]


def test_installed_but_unreachable_keeps_retrying(env: Path) -> None:
    printed: list[str] = []
    launchd = FakeLaunchd(True)

    async def body() -> None:
        link = DaemonLink("work", "w-1", launchd=launchd, err=printed.append, **FAST)
        assert await link.connect() is False
        assert link.state == "retrying"
        link.start(reg(), no_cmd)
        await asyncio.sleep(0.2)
        # the daemon comes up later: the link registers by itself
        daemon = make_daemon()
        await daemon.start()
        try:
            await wait_until(lambda: link.registered, timeout=5)
            assert "w-1" in daemon.wrapper_conns and link.state == "connected"
        finally:
            await link.close(0)
            await daemon.stop()

    asyncio.run(body())
    assert launchd.starts == 1
    assert printed == ["ccs: supervisor not responding — running unsupervised"]


def test_stopped_daemon_is_started_then_registers(env: Path) -> None:
    async def body() -> None:
        daemon = make_daemon()
        pending: list[asyncio.Task[None]] = []
        launchd = FakeLaunchd(
            True, on_start=lambda: pending.append(asyncio.ensure_future(daemon.start()))
        )
        link = DaemonLink("work", "w-2", launchd=launchd, **FAST)
        try:
            assert await link.connect() is True
            assert launchd.starts == 1
            link.start(reg("w-2"), no_cmd)
            await wait_until(lambda: link.registered)
            assert "w-2" in daemon.sessions
            assert paths.session_file("w-2").exists()
        finally:
            await link.close(0)
            await daemon.stop()

    asyncio.run(body())


def test_register_unregister_and_supervision_reply(env: Path) -> None:
    replies: list[dict[str, Any]] = []

    async def body(daemon: Daemon) -> None:
        link = DaemonLink("work", "w-3", **FAST)
        assert await link.connect()
        status = await link.profile_status()
        assert status is not None and status["profiles"][0]["id"] == "work"
        link.start(reg("w-3", overridden=True), no_cmd, replies.append)
        await wait_until(lambda: link.registered)
        rec = daemon.sessions["w-3"]
        assert rec["claude_pid"] == 222 and rec["cwd"] == "/tmp"
        await link.close(7)
        await wait_until(lambda: "w-3" not in daemon.sessions)

    run_with_daemon(body)
    assert replies and replies[0]["ok"] is True
    assert replies[0]["supervision"]["state"] == "running"


def test_started_overridden_sent_only_on_first_registration(env: Path) -> None:
    seen: list[bool] = []

    def install(daemon: Daemon) -> None:
        daemon.hooks.on_wrapper_registered(lambda info: seen.append(info.started_overridden))

    async def body() -> None:
        daemon = make_daemon(extensions=[install])
        await daemon.start()
        link = DaemonLink("work", "w-4", **FAST)
        await link.connect()
        link.start(reg("w-4", overridden=True), no_cmd)
        await wait_until(lambda: len(seen) == 1)
        await daemon.stop()  # daemon killed mid-session
        await wait_until(lambda: not link.registered)
        daemon2 = make_daemon(extensions=[install])
        await daemon2.start()
        try:
            await wait_until(lambda: "w-4" in daemon2.wrapper_conns and link.registered, timeout=5)
        finally:
            await link.close(0)
            await daemon2.stop()

    asyncio.run(body())
    assert seen == [True, False]


def test_commands_are_acked(env: Path) -> None:
    handled: list[dict[str, Any]] = []

    async def on_cmd(cmd: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        handled.append(cmd)
        return "injected", {"was_busy": True}

    async def body(daemon: Daemon) -> None:
        link = DaemonLink("work", "w-5", **FAST)
        await link.connect()
        link.start(reg("w-5"), on_cmd)
        await wait_until(lambda: "w-5" in daemon.wrapper_conns)
        ack = await daemon.send_cmd("w-5", "pause", {"holds": ["session"], "resume_at": None})
        assert ack["result"] == "injected" and ack["detail"] == {"was_busy": True}
        ack2 = await daemon.send_cmd("w-5", "resume", {"prompt": "go"})
        assert ack2["result"] == "injected"
        await link.close(0)

    run_with_daemon(body)
    assert [c["type"] for c in handled] == ["pause", "resume"]


def test_wrapper_event_reaches_hooks(env: Path) -> None:
    events: list[dict[str, Any]] = []

    def install(daemon: Daemon) -> None:
        daemon.hooks.on_wrapper_event(lambda info, msg: events.append(msg))

    async def body(daemon: Daemon) -> None:
        link = DaemonLink("work", "w-6", **FAST)
        await link.connect()
        link.start(reg("w-6"), no_cmd)
        await wait_until(lambda: link.registered)
        await link.wrapper_event("input_submitted_while_paused", {})
        await link.close(0)

    run_with_daemon(body, extensions=[install])
    assert events[0]["kind"] == "input_submitted_while_paused"
    assert events[0]["wrapper_id"] == "w-6"


def test_refused_registration_is_retried(env: Path, tmp_path: Path) -> None:
    """A refused `register_wrapper` (here: the profile isn't in the config yet) no longer ends
    supervision: the link drops that connection and registers on a later attempt."""
    late_dir = tmp_path / "profile-late"
    late_dir.mkdir()

    def add_profile(raw: dict[str, Any]) -> None:
        raw["profiles"].append(
            {
                "id": "late",
                "flag": "late",
                "name": "Late",
                "emoji": "L",
                "config_dir": str(late_dir),
            }
        )

    async def body(daemon: Daemon) -> None:
        link = DaemonLink("late", "w-late", **FAST)
        assert await link.connect()
        registration = Registration(
            wrapper_id="w-late",
            profile_id="late",
            wrapper_pid=111,
            claude_pid=222,
            cwd="/tmp",
            started_overridden=False,
        )
        link.start(registration, no_cmd)
        await wait_until(lambda: link.state == "retrying")
        assert not link.registered
        store.save(add_profile)
        await wait_until(lambda: link.registered, timeout=5)
        assert "w-late" in daemon.sessions and link.state == "connected"
        await link.close(0)

    run_with_daemon(body, config_scan_s=3600)


def test_permanent_refusal_closes_the_connection(
    env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def body(daemon: Daemon) -> None:
        link = DaemonLink("work", "w-bad", **FAST)
        assert await link.connect()
        bad = Registration(
            wrapper_id="w-bad",
            profile_id="work",
            wrapper_pid=111,
            claude_pid=0,  # not a pid: `bad_request`, the same request can never succeed
            cwd="/tmp",
            started_overridden=False,
        )
        link.start(bad, no_cmd)
        await wait_until(lambda: link.state == "unsupervised")
        assert link.client is None and not link.connected_event.is_set()
        await wait_until(lambda: not any(c.client == "launcher" for c in daemon.conns))
        await asyncio.sleep(0.3)  # no retries
        assert not any(c.client == "launcher" for c in daemon.conns)
        await link.close(0)

    with caplog.at_level(logging.WARNING, logger="ccs.launcher.daemon_link"):
        run_with_daemon(body)
    assert "bad_request" in caplog.text and "unsupervised" in caplog.text
