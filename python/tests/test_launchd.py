"""LaunchAgent: golden plist and launchctl argv via an injected runner (no real launchd)."""

from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

import pytest

from ccs.daemon.launchd import (
    LABEL,
    Launchd,
    LaunchdError,
    RunResult,
    install_env,
    parse_print,
    render_plist,
)

GOLDEN = Path(__file__).parent / "golden" / "daemon.plist"


class FakeRunner:
    def __init__(self, loaded: bool = False) -> None:
        self.loaded = loaded
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        verb = argv[1]
        if verb == "print":
            if self.loaded:
                return RunResult(0, "\tstate = running\n\tpid = 4242\n", "")
            return RunResult(113, "", "Could not find service")
        if verb == "bootstrap":
            self.loaded = True
        if verb == "bootout":
            was = self.loaded
            self.loaded = False
            return RunResult(0 if was else 3, "", "")
        return RunResult(0, "", "")


def test_render_plist_matches_golden() -> None:
    data = render_plist(
        "/Users/me/.local/bin/ccs",
        {"PATH": "/usr/bin:/bin", "HOME": "/Users/me"},
        Path("/Users/me/.local/state/ccs"),
    )
    assert data == GOLDEN.read_bytes()
    doc = plistlib.loads(data)
    assert doc["Label"] == LABEL
    assert doc["ProgramArguments"] == ["/Users/me/.local/bin/ccs", "daemon", "run", "--launchd"]
    assert doc["KeepAlive"] is True and doc["RunAtLoad"] is True
    assert doc["ProcessType"] == "Interactive"


def test_install_env_keeps_only_passthrough() -> None:
    env = install_env({"PATH": "/p", "HOME": "/h", "SECRET": "x", "XDG_STATE_HOME": "/s"})
    assert env == {"PATH": "/p", "HOME": "/h", "XDG_STATE_HOME": "/s"}


def test_parse_print() -> None:
    assert parse_print("a\n\tstate = running\n\tpid = 12\n") == {"state": "running", "pid": 12}
    assert parse_print("") == {"state": None, "pid": None}


@pytest.fixture
def ld(tmp_path: Path, tmp_xdg: object) -> tuple[Launchd, FakeRunner]:
    runner = FakeRunner()
    return Launchd(runner=runner, plist=tmp_path / "agents" / f"{LABEL}.plist"), runner


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def test_install_writes_plist_and_bootstraps(ld: tuple[Launchd, FakeRunner]) -> None:
    launchd, runner = ld
    launchd.install(ccs_exec="/x/ccs", env={"PATH": "/p", "HOME": "/h"})
    assert launchd.plist.exists()
    assert plistlib.loads(launchd.plist.read_bytes())["ProgramArguments"][0] == "/x/ccs"
    assert runner.calls == [
        ["launchctl", "bootout", target()],
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(launchd.plist)],
    ]


def test_install_writes_plist_durably(
    ld: tuple[Launchd, FakeRunner], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plist is fsynced (file and directory) before the rename, so power loss can't
    leave a truncated LaunchAgent."""
    launchd, _ = ld
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    launchd.install(ccs_exec="/x/ccs", env={"PATH": "/p", "HOME": "/h"})
    assert len(synced) >= 2  # the plist and its directory
    assert stat.S_IMODE(launchd.plist.stat().st_mode) == 0o644
    assert sorted(p.name for p in launchd.plist.parent.iterdir()) == [launchd.plist.name]
    assert plistlib.loads(launchd.plist.read_bytes())["Label"] == LABEL


class ShuttingDownRunner(FakeRunner):
    """`bootstrap` fails with EIO a few times, like while the old daemon is still exiting."""

    def __init__(self, failures: int) -> None:
        super().__init__(loaded=True)
        self.failures = failures

    def __call__(self, argv: list[str]) -> RunResult:
        if argv[1] == "bootstrap" and self.failures > 0:
            self.failures -= 1
            self.calls.append(argv)
            return RunResult(5, "", "Bootstrap failed: 5: Input/output error")
        return super().__call__(argv)


def test_install_retries_bootstrap_while_the_old_daemon_exits(
    tmp_path: Path, tmp_xdg: object
) -> None:
    runner = ShuttingDownRunner(failures=2)
    sleeps: list[float] = []
    launchd = Launchd(runner=runner, plist=tmp_path / f"{LABEL}.plist", sleep=sleeps.append)
    launchd.install(ccs_exec="/x/ccs", env={})
    assert [c[1] for c in runner.calls] == ["bootout", "bootstrap", "bootstrap", "bootstrap"]
    assert sleeps == [1.0, 1.0]
    assert runner.loaded


def test_install_gives_up_after_the_last_attempt(tmp_path: Path, tmp_xdg: object) -> None:
    runner = ShuttingDownRunner(failures=100)
    launchd = Launchd(runner=runner, plist=tmp_path / f"{LABEL}.plist", sleep=lambda s: None)
    with pytest.raises(LaunchdError, match="Input/output error"):
        launchd.install(ccs_exec="/x/ccs", env={})
    assert [c[1] for c in runner.calls].count("bootstrap") == 15


def test_start_bootstraps_when_unloaded_else_kickstarts(ld: tuple[Launchd, FakeRunner]) -> None:
    launchd, runner = ld
    with pytest.raises(LaunchdError):
        launchd.start()  # not installed
    launchd.install(ccs_exec="/x/ccs", env={})
    launchd.stop()
    runner.calls.clear()
    launchd.start()
    assert runner.calls[-1][:2] == ["launchctl", "bootstrap"]
    runner.calls.clear()
    launchd.start()
    assert runner.calls[-1] == ["launchctl", "kickstart", target()]


def test_restart_and_uninstall(ld: tuple[Launchd, FakeRunner]) -> None:
    launchd, runner = ld
    launchd.install(ccs_exec="/x/ccs", env={})
    runner.calls.clear()
    launchd.restart()
    assert runner.calls[-1] == ["launchctl", "kickstart", "-k", target()]
    launchd.uninstall()
    assert not launchd.plist.exists()
    assert runner.calls[-1] == ["launchctl", "bootout", target()]


def test_status_reports_loaded_and_probe(ld: tuple[Launchd, FakeRunner]) -> None:
    launchd, _ = ld
    launchd.install(ccs_exec="/x/ccs", env={})
    status = launchd.status(probe=lambda: {"responsive": True, "version": "9", "uptime_s": 5})
    assert status["installed"] is True and status["loaded"] is True
    assert status["pid"] == 4242 and status["state"] == "running"
    assert status["responsive"] is True and status["version"] == "9"
    launchd.uninstall()
    status = launchd.status(probe=lambda: None)
    assert status == {
        "installed": False,
        "loaded": False,
        "state": None,
        "pid": None,
        "responsive": False,
        "version": None,
        "uptime_s": None,
        "latency_ms": None,
    }
