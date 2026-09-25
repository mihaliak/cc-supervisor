"""`make install/upgrade/uninstall` scripts (P13) in an isolated HOME with stubbed system tools.

launchctl, pipx, open, osascript and the app binary are shell stubs that log their arguments;
`ccs` is this checkout run with the test interpreter. Nothing outside the temp HOME is touched.
"""

from __future__ import annotations

import json
import os
import plistlib
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
        self,
        script: str,
        *,
        extra: dict[str, str] | None = None,
        stdin: str = "",
        args: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **(extra or {})}
        return subprocess.run(
            [BASH, str(SCRIPTS / script), *(args or [])],
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


def test_uninstall_without_a_working_ccs_restores_from_bookkeeping(box: Box) -> None:
    installed(box)
    settings = box.home / ".claude-work" / "settings.json"
    broken = write_exec(box.root / "broken-ccs", "#!/nonexistent/python\n")  # venv gone
    done = box.run("uninstall.sh", extra={"YES": "1", "CCS": str(broken)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(settings.read_text(encoding="utf-8"))["statusLine"] == {
        "type": "command",
        "command": "bash old.sh",
    }
    assert "work: statusLine restored" in done.stdout
    assert not (box.state_dir / "statusline" / "work.json").exists()
    assert "nothing applied" not in done.stdout
    assert not box.plist.exists()  # removed without ccs too


@pytest.mark.parametrize("change", ["remove_profile", "invalid_config"])
def test_uninstall_reverts_what_ccs_refuses_to(box: Box, change: str) -> None:
    installed(box)
    cfg = json.loads(box.config_file.read_text(encoding="utf-8"))
    if change == "remove_profile":
        cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != "work"]
        cfg["default_profile"] = cfg["profiles"][0]["id"]
    else:
        cfg["profiles"][1]["limits"]["session"]["warn"] = 95  # above pause: config invalid
    box.config_file.write_text(json.dumps(cfg), encoding="utf-8")
    assert box.ccs("statusline", "revert", "--profile", "work").returncode != 0
    settings = box.home / ".claude-work" / "settings.json"
    done = box.run("uninstall.sh", extra={"YES": "1", "PURGE": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        json.loads(settings.read_text(encoding="utf-8"))["statusLine"]["command"] == "bash old.sh"
    )
    assert "work: statusLine restored" in done.stdout
    assert not box.state_dir.exists()
    assert not (box.home / ".claude-work" / "ccs-statusline.py").exists()


def test_uninstall_finds_ccs_on_path(box: Box) -> None:
    installed(box)
    stubs = Path(box.env["PATH"].split(":")[0])
    logging_stub(stubs / "ccs", box.stub_log, f'exec "{box.env["CCS"]}" "$@"')
    done = box.run("uninstall.sh", extra={"YES": "1", "CCS": ""})
    assert done.returncode == 0, done.stdout + done.stderr
    calls = box.calls()
    assert "ccs statusline revert --profile work" in calls
    assert "ccs daemon uninstall" in calls


def test_uninstall_purge_keeps_records_it_could_not_revert(box: Box) -> None:
    installed(box)
    settings = box.home / ".claude-work" / "settings.json"
    mine = {"statusLine": {"type": "command", "command": "bash mine.sh"}}
    settings.write_text(json.dumps(mine), encoding="utf-8")  # changed by hand since apply
    book = box.state_dir / "statusline" / "work.json"
    for extra in ({"YES": "1", "PURGE": "1"}, {"YES": "1", "PURGE": "1", "CCS": "/missing"}):
        done = box.run("uninstall.sh", extra=extra)
        assert done.returncode == 0, done.stdout + done.stderr
        assert json.loads(settings.read_text(encoding="utf-8")) == mine
        assert book.is_file()
        assert json.loads(book.read_text(encoding="utf-8"))["previous_statusline"] == {
            "type": "command",
            "command": "bash old.sh",
        }
        assert f"      {book}" in done.stdout
        assert "not reverted" in done.stdout and "nothing applied" not in done.stdout
        assert sorted(p.name for p in box.state_dir.rglob("*")) == ["statusline", "work.json"]
        assert not box.config_file.exists()


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
    assert any(c.startswith("launchctl bootstrap") for c in calls)
    assert f"open {box.app}" in calls


def test_upgrade_reinstalls_the_launchagent(box: Box) -> None:
    assert box.run("install.sh").returncode == 0
    old = plistlib.loads(box.plist.read_bytes())
    old["ProgramArguments"] = [a for a in old["ProgramArguments"] if a != "--launchd"]
    box.plist.write_bytes(plistlib.dumps(old))  # a LaunchAgent from an older ccs
    box.stub_log.unlink()
    state = str(box.state_dir)
    done = box.run("upgrade.sh", extra={"CCS_STATE_DIR": state})
    assert done.returncode == 0, done.stdout + done.stderr
    new = plistlib.loads(box.plist.read_bytes())
    assert new["ProgramArguments"][-1] == "--launchd"
    assert new["EnvironmentVariables"]["CCS_STATE_DIR"] == state
    calls = box.calls()
    bootout = next(i for i, c in enumerate(calls) if c.startswith("launchctl bootout"))
    bootstrap = next(i for i, c in enumerate(calls) if c.startswith("launchctl bootstrap"))
    assert bootout < bootstrap
    assert not any("kickstart" in c for c in calls)


def failing_ccs(box: Box, command: str) -> str:
    """A ccs that fails `ccs <command> …` and runs the real one for everything else."""
    real = box.env["CCS"]
    body = f'case "$1 $2" in "{command}") echo "boom" >&2; exit 3;; esac\nexec "{real}" "$@"\n'
    return str(write_exec(box.root / "failing-ccs", "#!/bin/sh\n" + body))


def stale_work_script(box: Box) -> Path:
    assert box.run("install.sh").returncode == 0
    generated = box.ccs("statusline", "generate", "--profile", "work")
    assert generated.returncode == 0, generated.stdout + generated.stderr
    script = box.home / ".claude-work" / "ccs-statusline.py"
    script.write_text("# stale\n", encoding="utf-8")
    return script


def test_upgrade_fails_when_profiles_cannot_be_listed(box: Box) -> None:
    script = stale_work_script(box)
    done = box.run("upgrade.sh", extra={"CCS": failing_ccs(box, "profile list")})
    assert done.returncode == 1, done.stdout + done.stderr
    assert "none to refresh" not in done.stdout
    assert "cannot list the profiles" in done.stderr
    assert "upgrade incomplete" in done.stderr
    assert "==> ccs doctor" in done.stdout  # still diagnoses
    assert script.read_text(encoding="utf-8") == "# stale\n"


def test_upgrade_fails_when_a_script_is_not_regenerated(box: Box) -> None:
    stale_work_script(box)
    done = box.run("upgrade.sh", extra={"CCS": failing_ccs(box, "statusline generate")})
    assert done.returncode == 1, done.stdout + done.stderr
    assert "! work: not regenerated" in done.stdout
    assert "none to refresh" not in done.stdout


def test_upgrade_stop_daemon_unloads_it_without_ccs(box: Box) -> None:
    assert box.run("install.sh").returncode == 0
    box.stub_log.unlink()
    done = box.run("upgrade.sh", extra={"CCS": "/missing"}, args=["--stop-daemon"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"launchctl bootout gui/{os.getuid()}/{LABEL}" in box.calls()
    assert not any(c.startswith("launchctl bootstrap") for c in box.calls())
    assert box.plist.exists()  # kept: upgrade.sh reinstalls and starts it
    assert "stopped" in done.stdout


def test_upgrade_stop_daemon_when_not_installed(box: Box) -> None:
    done = box.run("upgrade.sh", extra={"CCS": "/missing"}, args=["--stop-daemon"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "not installed" in done.stdout
    assert not any("bootout" in c for c in box.calls())


def test_make_upgrade_stops_the_daemon_around_the_pipx_reinstall(tmp_path: Path) -> None:
    # dry run (`make -n`): recipes are printed, not run (sub-makes dry-run too)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "PY": sys.executable}
    done = subprocess.run(
        ["make", "-n", "upgrade"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()

    def first(fragment: str) -> int:
        return next(i for i, line in enumerate(lines) if fragment in line)

    build = first("xcodebuild -project")
    stop = first("scripts/upgrade.sh --stop-daemon")
    pipx = first("pipx install --editable ./python --force")
    restart = max(i for i, line in enumerate(lines) if line.endswith("bash scripts/upgrade.sh"))
    assert build < stop < pipx < restart


def test_make_install_stops_a_running_daemon_around_the_pipx_reinstall(tmp_path: Path) -> None:
    # re-running `make install` over an existing install replaces the daemon's venv too
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "PY": sys.executable}
    done = subprocess.run(
        ["make", "-n", "install"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()

    def first(fragment: str) -> int:
        return next(i for i, line in enumerate(lines) if fragment in line)

    stop = first("scripts/upgrade.sh --stop-daemon")
    pipx = first("pipx install --editable ./python --force")
    start = first("bash scripts/install.sh")
    assert stop < pipx < start


# ---------------------------------------------------------------- uninstall.sh purge guard


@pytest.mark.parametrize("case", ["home", "home_parent", "home_symlink", "no_markers", "config"])
def test_uninstall_purge_refuses_dirs_that_are_not_ccs(box: Box, case: str) -> None:
    precious = box.home / "precious.txt"
    precious.write_text("keep me\n", encoding="utf-8")
    extra = {"YES": "1", "PURGE": "1", "CCS": str(box.root / "missing-ccs")}
    if case == "home":
        extra["CCS_STATE_DIR"] = str(box.home)
    elif case == "home_parent":
        extra["CCS_STATE_DIR"] = str(box.root)
    elif case == "home_symlink":
        (box.root / "state-link").symlink_to(box.home)
        extra["CCS_STATE_DIR"] = str(box.root / "state-link")
    elif case == "no_markers":
        docs = box.root / "documents"
        docs.mkdir()
        precious = docs / "notes.txt"
        precious.write_text("keep me\n", encoding="utf-8")
        extra["CCS_STATE_DIR"] = str(docs)
    else:
        xdg = box.root / "xdg"
        (xdg / "ccs").mkdir(parents=True)
        precious = xdg / "ccs" / "notes.txt"
        precious.write_text("keep me\n", encoding="utf-8")
        extra["XDG_CONFIG_HOME"] = str(xdg)
    done = box.run("uninstall.sh", extra=extra)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "! not deleting" in done.stdout
    assert "nothing deleted" in done.stdout
    assert precious.read_text(encoding="utf-8") == "keep me\n"
    assert (box.home / ".claude").is_dir() and (box.home / ".claude-work").is_dir()


def test_uninstall_purge_deletes_a_custom_state_dir(box: Box) -> None:
    state = box.root / "custom-state"
    extra = {"CCS_STATE_DIR": str(state)}
    assert box.run("install.sh", extra=extra).returncode == 0
    assert (state / "widget").is_dir()
    done = box.run("uninstall.sh", extra={**extra, "YES": "1", "PURGE": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert not state.exists()
    assert not box.config_file.exists()
    assert "not deleting" not in done.stdout


# ---------------------------------------------------------------- screenshots/capture_cli.sh


VENV_PY = REPO / "python" / ".venv" / "bin" / "python"


@pytest.mark.skipif(not VENV_PY.exists(), reason="needs python/.venv (make venv)")
@pytest.mark.parametrize(
    ("demo_arg", "out_arg"),
    [
        ("demo", "demo/out"),
        ("sub/../demo", "demo/out"),  # `..` in the demo dir
        ("demo", "./demo/./out"),
        ("demo", "link/out"),  # link -> demo
        ("link", "demo/out"),
    ],
)
def test_capture_cli_refuses_an_out_dir_inside_the_demo_dir(
    tmp_path: Path, demo_arg: str, out_arg: str
) -> None:
    demo = tmp_path / "demo"
    demo.mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "link").symlink_to(demo)
    done = subprocess.run(
        [BASH, str(SCRIPTS / "screenshots" / "capture_cli.sh"), demo_arg, out_arg],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "out-dir must be outside the demo dir" in done.stderr
    assert not (demo / "out").exists()


# ---------------------------------------------------------------- CI workflows


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    uses = [
        line.split("uses:", 1)[1].strip()
        for wf in sorted((REPO / ".github" / "workflows").glob("*.yml"))
        for line in wf.read_text(encoding="utf-8").splitlines()
        if "uses:" in line
    ]
    assert uses
    for ref in uses:
        action, _, rest = ref.partition("@")
        sha, _, comment = rest.partition(" # ")
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), ref
        assert comment.startswith("v"), f"{action}: add the release tag as a comment"


def test_scripts_are_valid_shell() -> None:
    for name in (
        "install.sh",
        "uninstall.sh",
        "upgrade.sh",
        "screenshots/capture_cli.sh",
        "screenshots/run.sh",
    ):
        assert subprocess.run([BASH, "-n", str(SCRIPTS / name)], check=False).returncode == 0
    assert subprocess.run(["/bin/sh", "-n", str(SCRIPTS / "check-prereqs.sh")]).returncode == 0
    assert os.access(SCRIPTS / "install.sh", os.X_OK)
