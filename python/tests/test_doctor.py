"""`ccs doctor` (P13): every check against temp dirs + the fake claude. Nothing real is touched."""

from __future__ import annotations

import json
import os
import plistlib
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ccs import cli, doctor, paths
from ccs.config import store
from ccs.config.defaults import default_config_dict, default_profile_dict
from ccs.config.models import Config
from ccs.config.validate import CLAUDE_LONG_OPTIONS
from ccs.daemon.launchd import LABEL, Launchd, RunResult
from ccs.fsio import atomic_write_json
from ccs.statusline import template
from ccs.usage.model import format_iso

NOW = datetime(2026, 9, 24, 17, 18, tzinfo=UTC)
HELP_FIXTURE = Path(__file__).parent / "fixtures" / "claude_help" / "claude-2.1.281.txt"
LOGGED_IN = Path(__file__).parent / "fixtures" / "auth_status" / "logged_in.json"


@dataclass
class World:
    home: Path
    config: Config
    env: doctor.Env
    launchctl_calls: list[list[str]]

    def profile_dir(self, pid: str) -> Path:
        prof = self.config.profile(pid)
        assert prof is not None
        return Path(prof.config_dir_env)


def iso(delta_s: float) -> str:
    out = format_iso(NOW - timedelta(seconds=delta_s))
    assert out is not None
    return out


def launchctl_runner(calls: list[list[str]], *, loaded: bool = True) -> Callable[..., RunResult]:
    def run(argv: list[str]) -> RunResult:
        calls.append(argv)
        if argv[1:2] == ["print"]:
            if not loaded:
                return RunResult(113, "", "Could not find service")
            return RunResult(0, f"{LABEL} = {{\n\tstate = running\n\tpid = {os.getpid()}\n}}\n", "")
        return RunResult(0, "", "")

    return run


def write_usage(pid: str, *, polled: float = 30, fetched: float = 30, status: str = "ok") -> None:
    atomic_write_json(
        paths.usage_file(pid),
        {
            "schema": 1,
            "profile_id": pid,
            "status": status,
            "error": "boom" if status != "ok" else None,
            "fetched_at": iso(fetched),
            "polled_at": iso(polled),
            "subscription_type": "max",
            "windows": {"session": None, "weekly": None, "model_scoped": []},
            "extra_usage": None,
        },
    )


@pytest.fixture
def world(
    tmp_xdg: Any,
    tmp_home: Path,
    fake_claude: Callable[[dict[str, Any]], Any],
    fake_claude_path: Path,
) -> World:
    """A healthy installation: config, two profiles, daemon up, fresh data, statusline applied."""
    fake_claude({"auth_status": {"stdout": LOGGED_IN.read_text(encoding="utf-8"), "exit": 0}})
    raw = default_config_dict()
    raw["claude_path"] = str(fake_claude_path)
    raw["profiles"] = []
    for pid, emoji in (("personal", "🏠"), ("work", "💼")):
        cfg_dir = tmp_home / f".claude-{pid}"
        cfg_dir.mkdir()
        raw["profiles"].append(default_profile_dict(pid, pid, pid.title(), emoji, str(cfg_dir)))
    config = store.create(raw)
    for prof in config.profiles:
        script = template.generate(prof, config).path
        settings = {"model": "opus", "statusLine": template.statusline_setting(script)}
        (Path(prof.config_dir_env) / "settings.json").write_text(json.dumps(settings) + "\n")
        write_usage(prof.id)
    atomic_write_json(paths.widget_snapshot(), {"schema": 1, "generated_at": iso(20)})
    app = tmp_home / "Applications" / doctor.APP_NAME
    app.mkdir(parents=True)
    plist = tmp_home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(
        plistlib.dumps(
            {"Label": LABEL, "EnvironmentVariables": {"PATH": str(fake_claude_path.parent)}}
        )
    )
    calls: list[list[str]] = []
    env = doctor.Env(
        launchd=Launchd(runner=launchctl_runner(calls), plist=plist),
        socket_probe=lambda: {
            "responsive": True,
            "version": doctor.__version__,
            "daemon_pid": os.getpid(),
            "latency_ms": 3.2,
        },
        daemon_status=lambda: {"ok": True, "daemon": {"app_connected": True}, "profiles": []},
        now=lambda: NOW,
        app_path=app,
    )
    return World(tmp_home, config, env, calls)


def run(env: doctor.Env) -> dict[str, doctor.Check]:
    checks, _ = doctor.run(env)
    return {f"{c.scope}|{c.id}": c for c in checks}


def by_id(checks: dict[str, doctor.Check], cid: str, scope: str = "global") -> doctor.Check:
    return checks[f"{scope}|{cid}"]


def not_ok(checks: dict[str, doctor.Check]) -> list[doctor.Check]:
    return [c for c in checks.values() if c.status != "ok"]


# ---------------------------------------------------------------- healthy


def test_healthy_installation_is_all_ok(world: World) -> None:
    checks = run(world.env)
    assert not_ok(checks) == []
    ids = {c.id for c in checks.values()}
    expected = {
        "python.version",
        "ccs.version",
        "claude.found",
        "claude.version",
        "config.valid",
        "config.reserved_flags",
        "daemon.installed",
        "daemon.running",
        "daemon.socket",
        "notifications.route",
        "app.installed",
        "widget.snapshot",
        "config_dir.exists",
        "auth.status",
        "auth.keychain_service",
        "usage.last_poll",
        "usage.status",
        "statusline.script",
        "statusline.interpreter",
        "statusline.applied",
        "supervisor.state",
    }
    assert expected <= ids
    assert by_id(checks, "statusline.applied", "profile:work").message == "applied"
    assert by_id(checks, "auth.status", "profile:work").message.startswith("signed in")


def test_doctor_never_writes(world: World) -> None:
    before = sorted(p for p in paths.state_dir().rglob("*"))
    cfg_before = paths.config_file().read_bytes()
    run(world.env)
    assert sorted(p for p in paths.state_dir().rglob("*")) == before
    assert paths.config_file().read_bytes() == cfg_before


# ---------------------------------------------------------------- global failures


def test_missing_config_is_a_fail_and_is_not_created(world: World) -> None:
    paths.config_file().unlink()
    checks = run(world.env)
    check = by_id(checks, "config.valid")
    assert check.status == "fail"
    assert "no config" in check.message
    assert check.fix is not None and "ccs profile list" in check.fix
    assert not any(c.scope.startswith("profile:") for c in checks.values())
    assert not paths.config_file().exists()


def test_invalid_config_lists_issues(world: World) -> None:
    raw = json.loads(paths.config_file().read_text())
    raw["profiles"][0]["limits"]["session"]["warn"] = 95
    paths.config_file().write_text(json.dumps(raw))
    check = by_id(run(world.env), "config.valid")
    assert check.status == "fail"
    assert "limits.session" in check.message
    assert check.fix == "ccs config validate"


def test_claude_not_found(world: World) -> None:
    raw = json.loads(paths.config_file().read_text())
    raw["claude_path"] = str(world.home / "nope" / "claude")
    paths.config_file().write_text(json.dumps(raw))
    checks = run(world.env)
    assert by_id(checks, "claude.found").status == "fail"
    assert by_id(checks, "claude.version").status == "warn"
    assert by_id(checks, "auth.status", "profile:work").status == "warn"


def test_daemon_path_without_claude_fails(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.loads(paths.config_file().read_text())
    raw["claude_path"] = None
    paths.config_file().write_text(json.dumps(raw))
    fake_bin = world.home / "bin"
    fake_bin.mkdir()
    (fake_bin / "claude").symlink_to(Path(__file__).parent / "fake_claude" / "claude")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    world.env.launchd.plist.write_bytes(
        plistlib.dumps({"Label": LABEL, "EnvironmentVariables": {"PATH": "/usr/bin:/bin"}})
    )
    checks = run(world.env)
    assert by_id(checks, "claude.found").status == "ok"
    check = by_id(checks, "claude.daemon_path")
    assert check.status == "fail"
    assert check.fix is not None and "ccs daemon install" in check.fix


def test_reserved_flags_collision_and_new_option(
    world: World, fake_claude: Callable[[dict[str, Any]], Any]
) -> None:
    logged_in = LOGGED_IN.read_text(encoding="utf-8")
    fake_claude(
        {
            "auth_status": {"stdout": logged_in, "exit": 0},
            "help": {"extra_options": ["--work"]},
        }
    )
    check = by_id(run(world.env), "config.reserved_flags")
    assert check.status == "fail"
    assert "--work (work)" in check.message
    assert check.fix == "ccs profile set work flag=<new-flag>"

    fake_claude(
        {
            "auth_status": {"stdout": logged_in, "exit": 0},
            "help": {"extra_options": ["--shiny-new"]},
        }
    )
    check = by_id(run(world.env), "config.reserved_flags")
    assert check.status == "warn"
    assert "--shiny-new" in check.message


def test_parse_help_matches_the_reserved_list() -> None:
    parsed = doctor.parse_help_options(HELP_FIXTURE.read_text(encoding="utf-8"))
    # `--all` belongs to `claude agents`, not the top-level help
    assert parsed == CLAUDE_LONG_OPTIONS - {"all"}


def test_daemon_socket_missing(world: World) -> None:
    env = replace(
        world.env,
        socket_probe=lambda: None,
        daemon_status=lambda: None,
        launchd=Launchd(
            runner=launchctl_runner(world.launchctl_calls, loaded=False),
            plist=world.env.launchd.plist,
        ),
    )
    checks = run(env)
    socket = by_id(checks, "daemon.socket")
    assert socket.status == "fail"
    assert socket.fix == "ccs daemon restart"
    running = by_id(checks, "daemon.running")
    assert running.status == "fail"
    assert running.fix == "ccs daemon start"
    assert by_id(checks, "notifications.route").status == "warn"


def test_daemon_not_installed(world: World) -> None:
    world.env.launchd.plist.unlink()
    env = replace(
        world.env,
        socket_probe=lambda: None,
        launchd=Launchd(
            runner=launchctl_runner(world.launchctl_calls, loaded=False),
            plist=world.env.launchd.plist,
        ),
    )
    checks = run(env)
    assert by_id(checks, "daemon.installed").status == "fail"
    assert by_id(checks, "daemon.running").fix == "ccs daemon install"
    assert by_id(checks, "daemon.socket").fix == "ccs daemon install"
    assert "global|claude.daemon_path" not in checks


def test_daemon_running_outside_launchd(world: World) -> None:
    env = replace(
        world.env,
        launchd=Launchd(
            runner=launchctl_runner(world.launchctl_calls, loaded=False),
            plist=world.env.launchd.plist,
        ),
    )
    check = by_id(run(env), "daemon.running")
    assert check.status == "ok"
    assert "outside launchd" in check.message


def test_daemon_version_mismatch_warns(world: World) -> None:
    env = replace(world.env, socket_probe=lambda: {"responsive": True, "version": "0.0.1"})
    check = by_id(run(env), "daemon.socket")
    assert check.status == "warn"
    assert check.fix == "ccs daemon restart"


def test_notifications_fallback_warns(world: World) -> None:
    env = replace(world.env, daemon_status=lambda: {"ok": True, "daemon": {"app_connected": False}})
    check = by_id(run(env), "notifications.route")
    assert check.status == "warn"
    assert "osascript" in check.message


def test_app_missing_warns(world: World) -> None:
    env = replace(world.env, app_path=world.home / "Applications" / "Nope.app")
    check = by_id(run(env), "app.installed")
    assert check.status == "warn"
    assert check.fix == "make app"


def test_stale_and_missing_snapshot(world: World) -> None:
    atomic_write_json(paths.widget_snapshot(), {"schema": 1, "generated_at": iso(600)})
    check = by_id(run(world.env), "widget.snapshot")
    assert check.status == "warn"
    assert "stale" in check.message
    paths.widget_snapshot().unlink()
    assert by_id(run(world.env), "widget.snapshot").status == "warn"


# ---------------------------------------------------------------- per profile


def test_missing_config_dir(world: World) -> None:
    for child in world.profile_dir("work").iterdir():
        child.unlink()
    world.profile_dir("work").rmdir()
    check = by_id(run(world.env), "config_dir.exists", "profile:work")
    assert check.status == "fail"


def test_logged_out(world: World, fake_claude: Callable[[dict[str, Any]], Any]) -> None:
    fake_claude({"auth_status": {"json": {"loggedIn": False, "authMethod": "none"}, "exit": 1}})
    check = by_id(run(world.env), "auth.status", "profile:work")
    assert check.status == "warn"
    assert check.fix == "ccs auth login --profile work"


def test_keychain_service_is_reported(world: World) -> None:
    check = by_id(run(world.env), "auth.keychain_service", "profile:work")
    assert check.status == "ok"
    assert check.message.startswith("Claude Code-credentials-")


@pytest.mark.parametrize(
    ("kwargs", "check_id", "status", "needle"),
    [
        ({"polled": 400, "fetched": 400}, "usage.last_poll", "warn", "not polling"),
        ({"polled": 30, "fetched": 900}, "usage.last_poll", "warn", "polls are failing"),
        ({"status": "needs_sign_in"}, "usage.status", "warn", "sign-in"),
        ({"status": "no_subscription"}, "usage.status", "warn", "subscription"),
        ({"status": "source_error"}, "usage.status", "fail", "boom"),
        ({"status": "stale"}, "usage.status", "warn", "stale"),
    ],
)
def test_usage_problems(
    world: World, kwargs: dict[str, Any], check_id: str, status: str, needle: str
) -> None:
    write_usage("work", **kwargs)
    check = by_id(run(world.env), check_id, "profile:work")
    assert check.status == status
    assert needle in check.message


def test_no_usage_file(world: World) -> None:
    paths.usage_file("work").unlink()
    checks = run(world.env)
    assert by_id(checks, "usage.last_poll", "profile:work").status == "warn"
    assert "profile:work|usage.status" not in checks


def test_stale_script_after_generator_bump(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(template, "GENERATOR_VERSION", template.GENERATOR_VERSION + 1)
    check = by_id(run(world.env), "statusline.script", "profile:work")
    assert check.status == "warn"
    assert check.fix == "ccs statusline generate --profile work"


def test_script_changed_profile_settings(world: World) -> None:
    raw = json.loads(paths.config_file().read_text())
    raw["profiles"][1]["emoji"] = "🧪"
    paths.config_file().write_text(json.dumps(raw))
    check = by_id(run(world.env), "statusline.script", "profile:work")
    assert check.status == "warn"
    assert "changed" in check.message


def test_missing_script_is_ok(world: World) -> None:
    (world.profile_dir("work") / template.SCRIPT_NAME).unlink()
    checks = run(world.env)
    assert by_id(checks, "statusline.script", "profile:work").status == "ok"
    assert by_id(checks, "statusline.interpreter", "profile:work").status == "ok"


def test_missing_interpreter_fails(world: World) -> None:
    prof = world.config.profile("work")
    assert prof is not None
    template.generate(prof, world.config, python=str(world.home / "gone" / "python3"))
    check = by_id(run(world.env), "statusline.interpreter", "profile:work")
    assert check.status == "fail"
    assert check.fix == "ccs statusline generate --profile work"


def test_statusline_not_applied_and_foreign(world: World) -> None:
    settings = world.profile_dir("work") / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    check = by_id(run(world.env), "statusline.applied", "profile:work")
    assert check.status == "ok"
    assert "not applied" in check.message

    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "bash ~/.claude/other.sh"}})
    )
    check = by_id(run(world.env), "statusline.applied", "profile:work")
    assert check.status == "ok"
    assert "different statusLine" in check.message


def test_statusline_applied_with_outdated_command(world: World) -> None:
    prof = world.config.profile("work")
    assert prof is not None
    script = template.script_path(prof)
    old = template.statusline_setting(script, python="/old/python3")
    (world.profile_dir("work") / "settings.json").write_text(json.dumps({"statusLine": old}))
    check = by_id(run(world.env), "statusline.applied", "profile:work")
    assert check.status == "warn"
    assert check.fix == "ccs statusline apply --profile work"


def test_statusline_disabled(world: World) -> None:
    raw = json.loads(paths.config_file().read_text())
    raw["profiles"][1]["statusline"]["enabled"] = False
    paths.config_file().write_text(json.dumps(raw))
    checks = run(world.env)
    assert by_id(checks, "statusline.script", "profile:work").message.startswith(
        "statusline disabled"
    )


def test_overdue_hold_warns(world: World) -> None:
    atomic_write_json(
        paths.supervisor_file("work"),
        {
            "schema": 1,
            "profile_id": "work",
            "holds": [
                {
                    "id": "session",
                    "kind": "session",
                    "instance": "session:x",
                    "scope": None,
                    "resets_at": iso(1200),
                },
                {
                    "id": "manual",
                    "kind": "manual",
                    "instance": "manual:y",
                    "scope": None,
                    "resets_at": None,
                },
            ],
            "warned": [],
            "paused_instances": [],
            "released_instances": [],
            "ledger": [],
        },
    )
    check = by_id(run(world.env), "supervisor.state", "profile:work")
    assert check.status == "warn"
    assert "session" in check.message and "manual" not in check.message


def test_active_hold_ok_and_bad_file_warns(world: World) -> None:
    doc = {"schema": 1, "profile_id": "work", "holds": [{"id": "manual", "resets_at": None}]}
    atomic_write_json(paths.supervisor_file("work"), doc)
    check = by_id(run(world.env), "supervisor.state", "profile:work")
    assert check.status == "ok"
    assert "manual" in check.message
    paths.supervisor_file("work").write_text("{not json")
    assert by_id(run(world.env), "supervisor.state", "profile:work").status == "warn"


# ---------------------------------------------------------------- runner, output, CLI


def test_check_timeout_is_a_fail_not_a_hang(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(ctx: doctor.Ctx) -> list[doctor.Check]:
        time.sleep(5)
        return []

    def crash(ctx: doctor.Ctx) -> list[doctor.Check]:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "GLOBAL_CHECKS", (("slow.check", slow), ("crash.check", crash)))
    started = time.monotonic()
    checks = run(replace(world.env, check_timeout=0.5))
    assert time.monotonic() - started < 3
    assert by_id(checks, "slow.check").status == "fail"
    assert "timed out" in by_id(checks, "slow.check").message
    assert by_id(checks, "crash.check").message == "check crashed: RuntimeError"


def test_subprocess_timeout_is_a_warn(world: World) -> None:
    def hung(argv: Any, env: Any, timeout: float) -> doctor.Ran:
        return doctor.Ran(-1, "", f"timed out after {timeout:.0f}s")

    checks = run(replace(world.env, run=hung))
    assert by_id(checks, "claude.version").status == "warn"
    assert by_id(checks, "config.reserved_flags").status == "warn"


def test_default_runner_times_out(tmp_path: Path) -> None:
    ran = doctor.default_runner(["/bin/sleep", "5"], None, 0.3)
    assert ran.rc == -1
    assert "timed out" in ran.stderr


def test_json_output_and_exit_code(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "ENV_FACTORY", lambda: world.env)
    assert cli.main(["doctor", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"checks", "summary"}
    assert set(doc["summary"]) == {"ok", "warn", "fail"}
    assert doc["summary"]["fail"] == 0
    for check in doc["checks"]:
        assert set(check) == {"id", "scope", "status", "message", "fix"}
        assert check["status"] in ("ok", "warn", "fail")

    failing = replace(world.env, socket_probe=lambda: None)
    monkeypatch.setattr(doctor, "ENV_FACTORY", lambda: failing)
    assert cli.main(["doctor", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["summary"]["fail"] >= 1


def test_human_output(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = replace(world.env, app_path=world.home / "missing.app")
    monkeypatch.setattr(doctor, "ENV_FACTORY", lambda: env)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Global\n")
    assert "Profile 💼 Work (work)" in out
    assert "  ! app.installed:" in out
    assert "      fix: make app" in out
    assert out.rstrip().endswith("1 warnings · 0 failed")


def test_app_fixture_matches_real_output_shape() -> None:
    """The Swift `DoctorResult` fixture uses real check ids and the real JSON keys."""
    fixture = Path(__file__).parents[2] / "schema" / "fixtures" / "ccs" / "doctor.json"
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    known = {cid for cid, _ in doctor.GLOBAL_CHECKS} | {cid for cid, _ in doctor.PROFILE_CHECKS}
    for check in doc["checks"]:
        assert set(check) == {"id", "scope", "status", "message", "fix"}
        assert check["id"] in known
