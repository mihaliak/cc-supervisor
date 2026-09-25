"""`ccs usage` end to end (fake claude, temp XDG dirs, optional fake daemon socket)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClaude, XdgDirs
from usage_helpers import payload

from ccs import paths
from ccs.cli import main
from ccs.config import store
from ccs.fsio import atomic_write_json
from ccs.usage.cli import display_width, extra_line, render_human
from ccs.usage.model import ExtraUsage
from ccs.usage.normalize import normalize
from ccs.usage.source_claude import write_snapshot

MakeFake = Callable[[dict[str, Any]], FakeClaude]


def write_config(tmp_path: Path, claude_path: Path | None = None) -> None:
    cdir = tmp_path / "profile-work"
    cdir.mkdir(exist_ok=True)
    raw: dict[str, Any] = {
        "version": 1,
        "revision": 0,
        "default_profile": "work",
        "claude_path": str(claude_path) if claude_path else None,
        "profiles": [
            {"id": "work", "flag": "work", "name": "Work", "emoji": "💼", "config_dir": str(cdir)}
        ],
    }
    store.create(raw)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main(["usage", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_no_data(tmp_xdg: XdgDirs, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path)
    code, out = run_json(capsys)
    assert code == 0 and out["ok"] is True
    assert out["profiles"] == [
        {"schema": 1, "profile_id": "work", "status": "no_data", "hint": "run: ccs usage --refresh"}
    ]


def test_direct_refresh_prints_without_writing(
    tmp_xdg: XdgDirs, tmp_path: Path, fake_claude: MakeFake, capsys: pytest.CaptureFixture[str]
) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path, fc.path)
    code, out = run_json(capsys, "--refresh")
    assert code == 0
    (entry,) = out["profiles"]
    assert entry["source"] == "direct_probe" and entry["status"] == "ok"
    expected = payload("ok_max.json")["rate_limits"]
    assert entry["windows"]["session"]["percent"] == expected["five_hour"]["utilization"]
    assert entry["windows"]["weekly"]["percent"] == expected["seven_day"]["utilization"]
    assert entry["windows"]["model_scoped"][0]["name"] == "Fable"
    assert not paths.usage_file("work").exists()  # the daemon is the single writer


def test_reads_file_and_merges_live_reports(
    tmp_xdg: XdgDirs, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    write_snapshot(
        normalize(payload("ok_max.json"), profile_id="work", fetched_at=now - timedelta(minutes=1))
    )
    later_reset = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    atomic_write_json(
        paths.live_file(wrapper_id="w1"),
        {
            "schema": 1,
            "profile_id": "work",
            "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rate_limits": {"five_hour": {"percent": 33, "resets_at": later_reset}},
        },
    )
    code, out = run_json(capsys)
    (entry,) = out["profiles"]
    assert code == 0 and entry["source"] == "file" and entry["status"] == "ok"
    assert entry["windows"]["session"] == {
        "percent": 33,
        "resets_at": later_reset,
        "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "statusline",
    }
    # human form
    assert main(["usage"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("💼 Work   Session  33%")
    assert "Weekly   52%" in text and "Fable     4%" in text
    assert "Extra usage off · €0.00 / €10.00" in text


def test_stale_file(tmp_xdg: XdgDirs, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path)
    old = datetime.now(UTC) - timedelta(minutes=30)
    write_snapshot(normalize(payload("ok_team.json"), profile_id="work", fetched_at=old))
    _, out = run_json(capsys)
    assert out["profiles"][0]["status"] == "stale"
    assert main(["usage", "--profile", "work"]) == 0
    assert "⚠ stale · updated 30m ago" in capsys.readouterr().out


def test_unknown_profile(
    tmp_xdg: XdgDirs, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path)
    code, out = run_json(capsys, "--profile", "nope")
    assert code == 2 and out["ok"] is False and "unknown profile" in out["error"]


# ------------------------------------------------------------- daemon refresh


@pytest.fixture
def short_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short state dir: unix socket paths must stay under ~104 bytes on macOS."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ccs-") as d:
        monkeypatch.setenv("CCS_STATE_DIR", d)
        yield Path(d)


def serve(sock_path: Path, on_refresh: Callable[[dict[str, Any]], None]) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def loop() -> None:
        conn, _ = srv.accept()
        with conn, conn.makefile("rwb") as fh:
            fh.write(b'{"event": {"type": "daemon.started"}}\n')  # pushed lines are skipped
            fh.flush()
            for line in fh:
                msg = json.loads(line)
                if msg["op"] == "refresh":
                    on_refresh(msg)
                fh.write((json.dumps({"id": msg["id"], "ok": True, "proto": 1}) + "\n").encode())
                fh.flush()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def test_refresh_via_daemon(
    tmp_xdg: XdgDirs, tmp_path: Path, short_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path)
    old = datetime.now(UTC) - timedelta(minutes=2)
    write_snapshot(normalize(payload("ok_team.json"), profile_id="work", fetched_at=old))
    seen: list[dict[str, Any]] = []

    def on_refresh(msg: dict[str, Any]) -> None:
        seen.append(msg)
        write_snapshot(
            normalize(payload("ok_max.json"), profile_id="work", fetched_at=datetime.now(UTC))
        )

    srv = serve(paths.daemon_sock(), on_refresh)
    try:
        code, out = run_json(capsys, "--refresh", "--profile", "work")
    finally:
        srv.close()
    assert code == 0
    assert seen == [{"proto": 1, "id": 2, "op": "refresh", "profile_id": "work"}]
    (entry,) = out["profiles"]
    assert entry["source"] == "daemon"
    assert entry["windows"]["session"]["percent"] == 15  # the file the daemon rewrote


def test_refresh_falls_back_when_socket_is_dead(
    tmp_xdg: XdgDirs,
    tmp_path: Path,
    short_state: Path,
    fake_claude: MakeFake,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fc = fake_claude({"stream": {"get_usage": {"mode": "ok"}}})
    write_config(tmp_path, fc.path)
    paths.daemon_sock().parent.mkdir(parents=True, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(paths.daemon_sock()))  # bound but never listening → connect refused
    stale.close()
    code, out = run_json(capsys, "--refresh")
    assert code == 0 and out["profiles"][0]["source"] == "direct_probe"
    assert os.path.exists(paths.daemon_sock())


# ------------------------------------------------------------- pure render helpers


def test_display_width() -> None:
    assert display_width("💼 Work") == 7
    assert display_width("🏠 Personal") == 11
    assert display_width("❤️ x") == 3  # VS16 has no width


def test_extra_line_variants() -> None:
    assert (
        extra_line(ExtraUsage(True, 32, 3.2, 10.0, "EUR", None))
        == "Extra usage 32% · €3.20 / €10.00"
    )
    assert extra_line(ExtraUsage(False, 0, 0.0, 10.0, "EUR", "out_of_credits")) == (
        "Extra usage off · €0.00 / €10.00"
    )
    assert extra_line(ExtraUsage(False, None, None, None, None, None)) == "Extra usage off"
    assert (
        extra_line(ExtraUsage(True, 5, 1.0, 20.0, "CHF", None))
        == "Extra usage 5% · 1.00 CHF / 20.00 CHF"
    )


def test_render_human_status_lines(tmp_path: Path) -> None:
    from ccs.config.models import Profile
    from ccs.usage.model import UsageSnapshot

    prof = Profile.from_dict(
        {"id": "p", "flag": "p", "name": "P", "emoji": "🏠", "config_dir": "~/.p"}
    )
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    text = render_human(
        [
            (prof, None),
            (prof, UsageSnapshot(profile_id="p", status="needs_sign_in")),
            (prof, UsageSnapshot(profile_id="p", status="source_error", error="boom")),
        ],
        now,
        UTC,
    )
    lines = text.splitlines()
    assert lines[0] == "🏠 P   no data yet · run: ccs usage --refresh"
    assert lines[1] == "🏠 P   ⚠ sign in required · run: ccs auth login --profile p"
    assert lines[2] == "🏠 P   ⚠ usage unavailable: boom"


def test_refresh_in_the_same_second_is_detected(
    tmp_xdg: XdgDirs,
    tmp_path: Path,
    short_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `polled_at` has whole seconds: a poll ending in the second of the previous one must
    # still count (by the file being rewritten), not burn the whole wait
    from ccs.clock import FakeClock
    from ccs.usage import cli as usage_cli

    second = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(usage_cli, "SystemClock", lambda: FakeClock(second))
    monkeypatch.setattr(usage_cli, "REFRESH_WAIT_S", 3.0)
    write_config(tmp_path)
    write_snapshot(normalize(payload("ok_team.json"), profile_id="work", fetched_at=second))

    def on_refresh(msg: dict[str, Any]) -> None:
        later = second + timedelta(milliseconds=400)  # same whole second
        write_snapshot(normalize(payload("ok_max.json"), profile_id="work", fetched_at=later))

    srv = serve(paths.daemon_sock(), on_refresh)
    started = time.monotonic()
    try:
        code, out = run_json(capsys, "--refresh", "--profile", "work")
    finally:
        srv.close()
    assert time.monotonic() - started < 2.0
    (entry,) = out["profiles"]
    assert code == 0 and entry["source"] == "daemon"
    assert entry["windows"]["session"]["percent"] == 15


def test_refresh_that_does_not_land_is_reported(
    tmp_xdg: XdgDirs,
    tmp_path: Path,
    short_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ccs.usage import cli as usage_cli

    monkeypatch.setattr(usage_cli, "REFRESH_WAIT_S", 0.5)
    write_config(tmp_path)
    old = datetime.now(UTC) - timedelta(minutes=2)
    write_snapshot(normalize(payload("ok_team.json"), profile_id="work", fetched_at=old))
    srv = serve(paths.daemon_sock(), lambda msg: None)  # answers, but never polls
    try:
        code, out = run_json(capsys, "--refresh", "--profile", "work")
    finally:
        srv.close()
    (entry,) = out["profiles"]
    assert code == 0 and entry["source"] == "daemon_pending"
    assert entry["polled_at"] == old.strftime("%Y-%m-%dT%H:%M:%SZ")  # the saved data
    paths.daemon_sock().unlink()
    srv = serve(paths.daemon_sock(), lambda msg: None)
    try:
        assert main(["usage", "--refresh", "--profile", "work"]) == 0
    finally:
        srv.close()
    assert "⚠ refresh not finished after 0.5 s · showing the last saved data" in (
        capsys.readouterr().out
    )


def test_render_human_shows_ended_windows_as_reset() -> None:
    # like the statusline: a window whose reset passed no longer applies (was "96% 23 Sep …")
    from ccs.config.models import Profile
    from ccs.usage.model import ScopedWindow, UsageSnapshot, Window

    prof = Profile.from_dict(
        {"id": "p", "flag": "p", "name": "P", "emoji": "🏠", "config_dir": "~/.p"}
    )
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    snap = UsageSnapshot(
        profile_id="p",
        status="ok",
        session=Window(40, now - timedelta(minutes=5), now - timedelta(hours=1)),
        weekly=Window(96, now - timedelta(days=1), now - timedelta(hours=1)),
        model_scoped=(ScopedWindow("Fable", 50, now + timedelta(hours=2), now),),
    )
    lines = render_human([(prof, snap)], now, UTC).splitlines()
    assert lines == [
        "🏠 P   Session   ?%  reset",
        "       Weekly    ?%  reset",
        "       Fable    50%  14:00 (in 2h)",
    ]
