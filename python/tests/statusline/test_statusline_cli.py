"""`ccs statusline generate|apply|revert|preview` and the daemon regeneration hook."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sl_helpers import make_config, new_config_dir, strip_ansi, write_config

from ccs.cli import main
from ccs.statusline import commands, daemon_ext, template


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    rc = main(["statusline", *argv, "--json"])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_generate_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    write_config(make_config(config_dir))
    rc, data = run_json(capsys, "generate", "--profile", "work")
    assert rc == 0
    assert data["ok"] is True
    assert data["profile_id"] == "work"
    assert data["script_path"] == str(config_dir / "ccs-statusline.py")
    assert data["generator_version"] == template.GENERATOR_VERSION
    assert data["changed"] is True
    rc, data = run_json(capsys, "generate", "--profile", "work")
    assert data["changed"] is False


def test_apply_and_revert_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    (config_dir / "settings.json").write_text('{"statusLine": {"type": "command", "command": "x"}}')
    write_config(make_config(config_dir))
    rc, data = run_json(capsys, "apply", "--profile", "work")
    assert rc == 0 and data["result"] == "applied"
    assert data["settings_path"] == str(config_dir / "settings.json")
    assert data["backup_path"] and Path(data["backup_path"]).is_file()
    assert data["command"].endswith(f"-S -E {config_dir / 'ccs-statusline.py'}")
    rc, data = run_json(capsys, "apply", "--profile", "work")
    assert rc == 0 and data["result"] == "already_applied" and data["backup_path"] is None
    rc, data = run_json(capsys, "revert", "--profile", "work")
    assert rc == 0 and data["result"] == "reverted"
    assert data["restored"] == {"type": "command", "command": "x"}
    rc, data = run_json(capsys, "revert", "--profile", "work")
    assert rc == 0 and data["result"] == "not_applied" and data["restored"] is None


def test_revert_conflict_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    write_config(make_config(config_dir))
    run_json(capsys, "apply", "--profile", "work")
    (config_dir / "settings.json").write_text('{"statusLine": {"type": "command", "command": "y"}}')
    rc, data = run_json(capsys, "revert", "--profile", "work")
    assert rc == 1
    assert data["ok"] is False and data["result"] == "conflict"


def test_apply_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    (config_dir / "settings.json").write_text("{broken")
    write_config(make_config(config_dir))
    rc, data = run_json(capsys, "apply", "--profile", "work")
    assert rc == 1 and data["ok"] is False
    assert data["issues"][0]["path"] == "invalid_settings"
    write_config(make_config(config_dir, statusline={"enabled": False}))
    rc, data = run_json(capsys, "apply", "--profile", "work")
    assert rc == 1 and data["issues"][0]["path"] == "statusline_disabled"


@pytest.mark.parametrize("action", ["apply", "revert"])
def test_value_errors_are_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    write_config(make_config(new_config_dir(tmp_path)))

    def bad_text(*_: Any, **__: Any) -> Any:
        raise UnicodeEncodeError("utf-8", "\ud83d", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(commands.apply_mod, action, bad_text)
    rc, data = run_json(capsys, action, "--profile", "work")
    assert rc == 1 and data["ok"] is False
    assert data["error"].startswith(f"{action} failed: ")
    assert main(["statusline", action, "--profile", "work"]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"ccs: {action} failed:") and "Traceback" not in err


def test_unknown_or_missing_profile(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    write_config(make_config(config_dir))
    rc, data = run_json(capsys, "preview", "--profile", "nope")
    assert rc == 2 and data["ok"] is False
    assert main(["statusline", "apply"]) == 2  # --profile is required
    capsys.readouterr()


def test_preview_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    write_config(make_config(config_dir))
    rc, data = run_json(capsys, "preview", "--profile", "work")
    assert rc == 0
    assert data["status"] == {
        "applied": False,
        "script_path": str(config_dir / "ccs-statusline.py"),
        "script_current": False,
    }
    states = [s["state"] for s in data["samples"]]
    assert states == list(commands.PREVIEW_STATES)
    by_state = {s["state"]: s for s in data["samples"]}
    assert by_state["normal"]["plain"].startswith("💼 Work ~ project ~ Opus 5.5 / xhigh ~ 45% ")
    assert "· limit approaching" in by_state["warn"]["plain"]
    assert "⏸ 90% ▓▓▓▓▓▓▓▓▓░ paused → resumes " in by_state["paused"]["plain"]
    assert by_state["overridden"]["plain"].endswith("· override")
    assert by_state["no_data"]["plain"].endswith("?% ░░░░░░░░░░")
    assert " ~ W ⚠ 86% " in by_state["weekly_warn"]["plain"]
    assert " ~ Fable ⚠ 82% " in by_state["model_scoped_warn"]["plain"]
    assert by_state["spill"]["plain"].endswith(" ~ € 3.20/10.00")
    assert by_state["supervisor_offline"]["plain"].endswith("⚠ supervisor offline")
    for sample in data["samples"]:
        assert strip_ansi(sample["ansi"]) == sample["plain"]
        assert "".join(seg["text"] for seg in sample["segments"]) == sample["plain"]


def test_preview_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    write_config(make_config(config_dir))
    assert main(["statusline", "preview", "--profile", "work"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(commands.PREVIEW_STATES)
    assert lines[0].startswith("normal ")


def test_human_generate_and_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = new_config_dir(tmp_path)
    write_config(make_config(config_dir))
    assert main(["statusline", "generate", "--profile", "work"]) == 0
    assert "(written)" in capsys.readouterr().out
    assert main(["statusline", "apply", "--profile", "work"]) == 0
    assert "statusLine →" in capsys.readouterr().out
    assert main(["statusline", "revert", "--profile", "work"]) == 0
    assert "restored" in capsys.readouterr().out


# ---------------------------------------------------------------- daemon hook


def test_daemon_refresh_only_existing_scripts(tmp_path: Path) -> None:
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir()
    other.mkdir()
    cfg = make_config(work)
    template.generate(cfg.profiles[0], cfg)
    assert daemon_ext.refresh_scripts(cfg) == []  # already current
    renamed = make_config(work, name="Job")
    assert daemon_ext.refresh_scripts(renamed) == ["work"]
    assert '"name": "Job"' in (work / "ccs-statusline.py").read_text()
    fresh = make_config(other)
    assert daemon_ext.refresh_scripts(fresh) == []
    assert not (other / "ccs-statusline.py").exists()  # never created by the daemon
    disabled = make_config(work, name="Off", statusline={"enabled": False})
    assert daemon_ext.refresh_scripts(disabled) == []


def test_daemon_hook_registration(tmp_path: Path) -> None:
    registered: list[Any] = []

    class Hooks:
        def on_config_changed(self, fn: Any) -> None:
            registered.append(fn)

    class FakeDaemon:
        hooks = Hooks()

    daemon_ext.install(FakeDaemon())  # type: ignore[arg-type]
    assert registered == [daemon_ext.on_config_changed]
    work = tmp_path / "w"
    work.mkdir()
    cfg = make_config(work)
    template.generate(cfg.profiles[0], cfg)
    asyncio.run(daemon_ext.on_config_changed(None, make_config(work, emoji="🛠")))
    assert '"emoji": "🛠"' in (work / "ccs-statusline.py").read_text()


def test_extension_is_listed() -> None:
    from ccs.daemon import extensions

    assert "ccs.statusline.daemon_ext" in extensions.EXTENSIONS
    assert extensions.resolve(["ccs.statusline.daemon_ext"]) == [daemon_ext.install]
