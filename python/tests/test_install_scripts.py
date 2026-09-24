"""`make install/upgrade/uninstall` scripts (P13) in an isolated HOME with stubbed system tools.

launchctl, pipx, open, osascript and the app binary are shell stubs that log their arguments;
`ccs` is this checkout run with the test interpreter. Nothing outside the temp HOME is touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
FAKE_CLAUDE = Path(__file__).parent / "fake_claude" / "claude"
LOGGED_IN = Path(__file__).parent / "fixtures" / "auth_status" / "logged_in.json"
LABEL = "local.ccsupervisor.daemon"
BASH = "/bin/bash"  # macOS system bash 3.2: the scripts must run on it


def write_exec(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def logging_stub(path: Path, log: Path, body: str = "exit 0") -> Path:
    return write_exec(path, f'#!/bin/sh\nprintf "%s\\n" "$(basename "$0") $*" >> "{log}"\n{body}\n')


@dataclass
class Box:
    root: Path
    home: Path
    stub_log: Path
    app: Path
    env: dict[str, str]

    def run(
        self, script: str, *, extra: dict[str, str] | None = None, stdin: str = ""
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **(extra or {})}
        return subprocess.run(
            [BASH, str(SCRIPTS / script)],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.stub_log.exists():
            return []
        return self.stub_log.read_text(encoding="utf-8").splitlines()

    def ccs(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.env["CCS"], *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    @property
    def config_file(self) -> Path:
        return self.home / ".config" / "ccs" / "config.json"

    @property
    def state_dir(self) -> Path:
        return self.home / ".local" / "state" / "ccs"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


@pytest.fixture
def box(tmp_path: Path) -> Box:
    home = tmp_path / "home"
    home.mkdir()
    for name in (".claude", ".claude-work"):
        (home / name).mkdir()
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "stub-calls.log"
    logging_stub(stubs / "launchctl", log, 'case "$1" in print) exit 113;; esac\nexit 0')
    logging_stub(stubs / "open", log)
    logging_stub(stubs / "osascript", log)
    logging_stub(stubs / "pipx", log)
    (stubs / "claude").symlink_to(FAKE_CLAUDE)
    ccs = write_exec(stubs / "ccs-wrapper", f'#!/bin/sh\nexec "{sys.executable}" -m ccs "$@"\n')
    app = home / "Applications" / "CC Supervisor.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    logging_stub(app / "Contents" / "MacOS" / "CC Supervisor", log)
    scenario = tmp_path / "fake-claude.json"
    scenario.write_text(
        json.dumps({"auth_status": {"stdout": LOGGED_IN.read_text(encoding="utf-8")}}),
        encoding="utf-8",
    )
    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "CCS": str(ccs),
        "APP": str(app),
        "OPEN": str(stubs / "open"),
        "OSASCRIPT": str(stubs / "osascript"),
        "PIPX": str(stubs / "pipx"),
        "PY": sys.executable,
        "WAIT_S": "1",
        "FAKE_CLAUDE_SCENARIO": str(scenario),
        "FAKE_CLAUDE_LOG": str(tmp_path / "fake-claude.log"),
        "CCS_OSASCRIPT": "true",
    }
    return Box(tmp_path, home, log, app, env)


def claude_dir_files(box: Box) -> dict[str, list[str]]:
    return {
        name: sorted(p.name for p in (box.home / name).iterdir())
        for name in (".claude", ".claude-work")
    }


# ---------------------------------------------------------------- check-prereqs.sh


def prereq_stubs(tmp_path: Path, *, xcodegen: bool) -> dict[str, str]:
    stubs = tmp_path / "prereq-bin"
    stubs.mkdir()
    write_exec(stubs / "sw_vers", "#!/bin/sh\necho 26.6.2\n")
    write_exec(stubs / "xcodebuild", "#!/bin/sh\necho 'Xcode 26.2'\necho 'Build version 17C52'\n")
    write_exec(stubs / "pipx", "#!/bin/sh\necho 1.17.5\n")
    write_exec(stubs / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    if xcodegen:
        write_exec(stubs / "xcodegen", "#!/bin/sh\necho 'Version: 2.46.0'\n")
    return {"PATH": f"{stubs}:/usr/bin:/bin", "PY": str(stubs / "python3"), "LANG": "en_US.UTF-8"}


def run_prereqs(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(SCRIPTS / "check-prereqs.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_prereqs_missing_xcodegen_fails_with_hint(tmp_path: Path) -> None:
    done = run_prereqs(prereq_stubs(tmp_path, xcodegen=False))
    assert done.returncode == 1
    assert "✗ xcodegen missing" in done.stdout
    assert "fix: brew install xcodegen" in done.stdout
    assert "! claude not on PATH" in done.stdout  # only a warning
    assert "✓ macOS 26.6.2" in done.stdout


def test_prereqs_all_present_passes(tmp_path: Path) -> None:
    done = run_prereqs(prereq_stubs(tmp_path, xcodegen=True))
    assert done.returncode == 0, done.stdout
    assert "✓ xcodegen 2.46.0" in done.stdout
    assert "✓ Xcode 26.2" in done.stdout


def test_prereqs_old_python_and_macos_fail(tmp_path: Path) -> None:
    env = prereq_stubs(tmp_path, xcodegen=True)
    stubs = Path(env["PATH"].split(":")[0])
    write_exec(stubs / "sw_vers", "#!/bin/sh\necho 15.4\n")
    write_exec(stubs / "python3", "#!/bin/sh\necho 3.9.6\nexit 1\n")
    done = run_prereqs(env)
    assert done.returncode == 1
    assert "✗ macOS 15.4: need macOS 26" in done.stdout
    assert "✗ Python 3.9.6" in done.stdout


# ---------------------------------------------------------------- install.sh


def test_install_in_isolated_home(box: Box) -> None:
    before = claude_dir_files(box)
    done = box.run("install.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    cfg = json.loads(box.config_file.read_text(encoding="utf-8"))
    assert [p["id"] for p in cfg["profiles"]] == ["personal", "work"]
    assert box.plist.exists()
    calls = box.calls()
    assert any(c.startswith("launchctl bootstrap gui/") for c in calls)
    assert "CC Supervisor --register-login-item" in calls
    assert f"open {box.app}" in calls
    assert "ccs doctor" in done.stdout or "==> ccs doctor" in done.stdout
    assert "Next steps" in done.stdout
    # never touches the Claude config dirs: no statusline script, no settings.json
    assert claude_dir_files(box) == before


def test_install_without_ccs_stops(box: Box) -> None:
    done = box.run("install.sh", extra={"CCS": str(box.root / "missing-ccs")})
    assert done.returncode == 1
    assert "make install-dev" in done.stderr


def test_install_is_idempotent(box: Box) -> None:
    assert box.run("install.sh").returncode == 0
    first = box.config_file.read_text(encoding="utf-8")
    assert box.run("install.sh").returncode == 0
    assert box.config_file.read_text(encoding="utf-8") == first


# ---------------------------------------------------------------- uninstall.sh


def installed(box: Box) -> None:
    assert box.run("install.sh").returncode == 0
    settings = box.home / ".claude-work" / "settings.json"
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "bash old.sh"}}) + "\n",
        encoding="utf-8",
    )
    applied = box.ccs("statusline", "apply", "--profile", "work")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "ccs-statusline.py" in settings.read_text(encoding="utf-8")


def test_uninstall_declined_changes_nothing(box: Box) -> None:
    installed(box)
    done = box.run("uninstall.sh", stdin="n\n")
    assert done.returncode == 1
    assert "aborted" in done.stdout
    assert box.plist.exists() and box.app.exists()


def test_uninstall_keeps_config_without_purge(box: Box) -> None:
    installed(box)
    settings = box.home / ".claude-work" / "settings.json"
    done = box.run("uninstall.sh", extra={"YES": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    # statusLine restored, daemon + app + login item + pipx package removed
    restored = json.loads(settings.read_text(encoding="utf-8"))
    assert restored["statusLine"]["command"] == "bash old.sh"
    assert not box.plist.exists()
    assert not box.app.exists()
    calls = box.calls()
    assert "CC Supervisor --unregister-login-item" in calls
    assert "pipx uninstall cc-supervisor" in calls
    assert any(c.startswith("osascript") for c in calls)
    # kept: config, state and the generated script (no PURGE)
    assert box.config_file.exists()
    assert box.state_dir.exists()
    assert (box.home / ".claude-work" / "ccs-statusline.py").exists()
    assert "kept" in done.stdout
    # the Claude config dirs themselves stay
    assert (box.home / ".claude").is_dir() and (box.home / ".claude-work").is_dir()


def test_uninstall_purge(box: Box) -> None:
    installed(box)
    done = box.run("uninstall.sh", extra={"YES": "1", "PURGE": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert not box.config_file.exists()
    assert not box.state_dir.exists()
    assert not (box.home / ".claude-work" / "ccs-statusline.py").exists()
    assert (box.home / ".claude-work" / "settings.json").exists()
    assert (box.home / ".claude").is_dir()


def test_uninstall_interactive_purge_prompt(box: Box) -> None:
    installed(box)
    done = box.run("uninstall.sh", stdin="y\ny\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not box.config_file.exists()


def test_uninstall_with_nothing_installed(box: Box) -> None:
    done = box.run("uninstall.sh", extra={"YES": "1", "CCS": str(box.root / "missing-ccs")})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "nothing applied by ccs" in done.stdout


# ---------------------------------------------------------------- upgrade.sh


def test_upgrade_refreshes_existing_scripts_only(box: Box) -> None:
    assert box.run("install.sh").returncode == 0
    generated = box.ccs("statusline", "generate", "--profile", "work")
    assert generated.returncode == 0, generated.stdout + generated.stderr
    script = box.home / ".claude-work" / "ccs-statusline.py"
    good = script.read_text(encoding="utf-8")
    script.write_text("# stale\n", encoding="utf-8")
    box.stub_log.unlink()
    done = box.run("upgrade.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    assert script.read_text(encoding="utf-8") == good
    assert not (box.home / ".claude" / "ccs-statusline.py").exists()
    calls = box.calls()
    assert any(c.startswith("launchctl bootstrap") or "kickstart" in c for c in calls)
    assert f"open {box.app}" in calls


def test_scripts_are_valid_shell() -> None:
    for name in ("install.sh", "uninstall.sh", "upgrade.sh"):
        assert subprocess.run([BASH, "-n", str(SCRIPTS / name)], check=False).returncode == 0
    assert subprocess.run(["/bin/sh", "-n", str(SCRIPTS / "check-prereqs.sh")]).returncode == 0
    assert os.access(SCRIPTS / "install.sh", os.X_OK)
