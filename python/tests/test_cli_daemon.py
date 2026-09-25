"""`ccs status`, `ccs events`, `ccs daemon …` end to end (fake claude, temp state, no launchd)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import ThreadedDaemon, scenario, short_state_dir, write_config

from ccs import paths
from ccs.cli import main
from ccs.config import store
from ccs.daemon import launchd
from ccs.daemon.cli import format_event
from ccs.daemon.client import DaemonClient, daemon_available
from ccs.events import Event, EventBus
from ccs.fsio import atomic_write_json
from ccs.usage.model import UsageSnapshot, Window
from ccs.usage.source_claude import write_snapshot


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    with short_state_dir(monkeypatch) as d:
        scenario(tmp_path, monkeypatch, {"stream": {"get_usage": {"mode": "ok"}}})
        write_config(tmp_path)
        paths.ensure_state_layout()
        yield d


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    assert code == 0, out
    data = json.loads(out)
    assert isinstance(data, dict)
    return data


def test_status_from_files_without_daemon(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    write_snapshot(
        UsageSnapshot(
            profile_id="work",
            status="ok",
            fetched_at=now,
            polled_at=now,
            session=Window(45, now + timedelta(hours=2), now),
        )
    )
    atomic_write_json(
        paths.session_file("w1"),
        {"schema": 1, "wrapper_id": "w1", "profile_id": "work", "supervision": {"state": "paused"}},
    )
    data = run_json(capsys, "status")
    assert data["ok"] is True
    assert data["daemon"] == {"responsive": False}
    (profile,) = data["profiles"]
    assert profile["usage"]["windows"]["session"]["percent"] == 45
    assert [s["wrapper_id"] for s in profile["sessions"]] == ["w1"]
    assert main(["status"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("daemon: not running")
    assert "Session" in text and "sessions: 1 supervised (1 paused)" in text


def test_status_via_daemon(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with ThreadedDaemon():
        data = run_json(capsys, "status")
        assert data["daemon"]["responsive"] is True
        assert data["profiles"][0]["id"] == "work"
        assert "id" not in data and "proto" not in data
        assert main(["status", "--profile", "nope", "--json"]) == 2
        capsys.readouterr()


def test_events_test_notification(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No daemon: a clear error, exit 1.
    assert main(["events", "--test", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    with ThreadedDaemon():
        data = run_json(capsys, "events", "--test")
        assert data["ok"] is True
        assert data["via"] == "osascript"  # no menu bar app connected in tests (osascript stubbed)
        assert main(["events", "--test"]) == 0
        assert "test notification sent" in capsys.readouterr().out
    events = [e for e in run_json(capsys, "events")["events"] if e["type"] == "notify.test"]
    assert events and events[-1]["data"]["notify"] is True


def test_events_tail_json_and_text(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = store.load()[0]
    bus = EventBus(config=lambda: cfg)
    bus.emit(Event("session.started", "work", None, {}))
    bus.emit(Event("usage.source_error", "work", "k", {"error": "boom"}))
    data = run_json(capsys, "events")
    assert [e["type"] for e in data["events"]] == ["session.started", "usage.source_error"]
    assert main(["events"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].endswith("usage.source_error  work  💼 Work: usage unavailable — boom")


def test_format_event_without_title_uses_key() -> None:
    line = format_event({"ts": None, "type": "limit.warn", "profile_id": None, "key": "k1"})
    assert line == "--:--:--  limit.warn  -  k1"


def test_daemon_logs(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths.daemon_log().write_text("\n".join(f"line {i}" for i in range(250)) + "\n")
    data = run_json(capsys, "daemon", "logs")
    assert len(data["lines"]) == 200 and data["lines"][-1] == "line 249"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> launchd.RunResult:
        self.calls.append(argv)
        if argv[1] == "print":
            return launchd.RunResult(113, "", "not found")
        return launchd.RunResult(0, "", "")


def test_daemon_status_and_install_use_runner(
    env: Path,
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rec = Recorder()
    monkeypatch.setattr(launchd, "default_runner", rec)
    data = run_json(capsys, "daemon", "status")
    assert data["installed"] is False and data["responsive"] is False
    data = run_json(capsys, "daemon", "install")
    assert data == {"ok": True, "action": "installed", "label": launchd.LABEL}
    plist = tmp_home / "Library" / "LaunchAgents" / f"{launchd.LABEL}.plist"
    assert plist.exists()
    assert ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)] in rec.calls
    assert main(["daemon", "stop"]) == 0
    capsys.readouterr()
    run_json(capsys, "daemon", "uninstall")
    assert not plist.exists()


def _ccs(*args: str) -> list[str]:
    return [sys.executable, "-m", "ccs", *args]


def test_daemon_run_subprocess_lifecycle(env: Path) -> None:
    proc = subprocess.Popen(
        _ccs("daemon", "run", "--foreground"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not daemon_available() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert daemon_available()
        second = subprocess.run(_ccs("daemon", "run"), capture_output=True, text=True, timeout=20)
        assert second.returncode == 0 and "daemon already running" in second.stdout
        with DaemonClient() as client:
            client.hello()
            assert client.request("status")["ok"] is True
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(20)
    assert proc.returncode == 0
    assert not paths.daemon_sock().exists()
    types = [json.loads(x)["type"] for x in paths.events_file().read_text().splitlines()]
    assert types[0] == "daemon.started" and types[-1] == "daemon.stopped"
    assert "daemon started" in paths.daemon_log().read_text()


def test_events_follow_via_daemon(env: Path) -> None:
    with ThreadedDaemon() as td:
        proc = subprocess.Popen(
            _ccs("events", "--follow", "--json"), stdout=subprocess.PIPE, text=True
        )
        try:
            time.sleep(1.5)
            assert td.daemon is not None
            daemon = td.daemon
            td.call(lambda: daemon.emit(Event("session.started", "work", None, {"n": 1})))
            assert proc.stdout is not None
            line = proc.stdout.readline()
            event = json.loads(line)
            assert event["type"] == "session.started" and event["data"]["n"] == 1
        finally:
            proc.terminate()
            proc.wait(10)


def test_follow_file_drops_a_stale_partial_on_rotation(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial line read from the old file must not be glued onto the new file's first."""
    from ccs.daemon import cli as daemon_cli

    log_path = paths.events_file()
    log_path.write_text(json.dumps({"type": "old", "n": 0}) + "\n", encoding="utf-8")

    def torn() -> None:  # read mid-write: the rest lands in the file after rotation
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write('{"type": "torn", "n')

    def rotate() -> None:
        os.replace(log_path, f"{log_path}.1")
        log_path.write_text(json.dumps({"type": "new", "n": 2}) + "\n", encoding="utf-8")

    steps = [torn, rotate]

    def fake_sleep(_: float) -> None:
        if not steps:
            raise KeyboardInterrupt
        steps.pop(0)()

    monkeypatch.setattr(daemon_cli.time, "sleep", fake_sleep)
    assert daemon_cli._follow_file(True) == 0
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert printed == [{"type": "new", "n": 2}]
