"""`ccs.statusline.apply`: patch / revert `settings.json` on temp config dirs only."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from sl_helpers import make_config, new_config_dir, write_config

from ccs import paths
from ccs.statusline import apply as ap
from ccs.statusline import template

OLD_STATUSLINE = {
    "type": "command",
    "command": "bash /Users/me/.claude/statusline-command.sh",
}


def write_settings(config_dir: Path, data: dict[str, Any], mode: int = 0o644) -> Path:
    path = config_dir / "settings.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(mode)
    return path


def existing_settings() -> dict[str, Any]:
    return {
        "includeCoAuthoredBy": False,
        "model": "opus[1m]",
        "statusLine": OLD_STATUSLINE,
        "tui": "fullscreen",
        "theme": "dark",
    }


def test_apply_fresh_config_dir(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    profile = cfg.profiles[0]
    result = ap.apply(profile, cfg)
    assert result.result == ap.APPLIED
    assert result.backup_path is None
    settings = json.loads((config_dir / "settings.json").read_text())
    command = template.statusline_command(config_dir / "ccs-statusline.py")
    assert settings == {"statusLine": {"type": "command", "command": command}}
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["previous_statusline"] is None
    assert book["backup_path"] is None
    assert book["command"] == command
    assert (config_dir / "ccs-statusline.py").is_file()
    assert ap.is_applied(profile)


def test_apply_existing_keeps_order_mode_and_backs_up(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings(), mode=0o600)
    original = path.read_text()
    cfg = make_config(config_dir)
    result = ap.apply(cfg.profiles[0], cfg)
    assert result.result == ap.APPLIED
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("settings.json.ccs-backup-")
    assert result.backup_path.read_text() == original
    after = path.read_text()
    assert after.endswith("\n")
    data = json.loads(after)
    assert list(data) == ["includeCoAuthoredBy", "model", "statusLine", "tui", "theme"]
    assert data["statusLine"]["command"] == result.command
    assert data["model"] == "opus[1m]"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["previous_statusline"] == OLD_STATUSLINE
    assert book["backup_path"] == str(result.backup_path)


def test_apply_is_idempotent(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    backups = sorted(config_dir.glob("settings.json.ccs-backup-*"))
    second = ap.apply(cfg.profiles[0], cfg)
    assert second.result == ap.ALREADY_APPLIED
    assert second.backup_path is None
    assert sorted(config_dir.glob("settings.json.ccs-backup-*")) == backups


def test_revert_restores_exact_previous(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    profile = cfg.profiles[0]
    ap.apply(profile, cfg)
    result = ap.revert(profile)
    assert result.result == ap.REVERTED
    assert result.restored == OLD_STATUSLINE
    assert json.loads(path.read_text()) == existing_settings()
    assert list(json.loads(path.read_text())) == list(existing_settings())
    assert not paths.statusline_file("work").exists()
    assert (config_dir / "ccs-statusline.py").is_file()  # the launcher still uses it
    assert ap.revert(profile).result == ap.NOT_APPLIED


def test_revert_removes_key_when_there_was_none(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, {"theme": "dark"})
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    assert ap.revert(cfg.profiles[0]).result == ap.REVERTED
    assert json.loads(path.read_text()) == {"theme": "dark"}


def test_revert_conflict_when_user_changed_it(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    data = json.loads(path.read_text())
    data["statusLine"] = {"type": "command", "command": "echo mine"}
    path.write_text(json.dumps(data))
    before = path.read_text()
    result = ap.revert(cfg.profiles[0])
    assert result.result == ap.CONFLICT
    assert result.hint and "manually" in result.hint
    assert path.read_text() == before
    assert paths.statusline_file("work").exists()


def test_invalid_json_is_never_touched(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = config_dir / "settings.json"
    path.write_text("{ not json")
    cfg = make_config(config_dir)
    with pytest.raises(ap.ApplyError) as err:
        ap.apply(cfg.profiles[0], cfg)
    assert err.value.code == "invalid_settings"
    assert path.read_text() == "{ not json"
    assert not paths.statusline_file("work").exists()
    assert list(config_dir.glob("settings.json.ccs-backup-*")) == []


def test_non_object_json_rejected(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    (config_dir / "settings.json").write_text("[]")
    cfg = make_config(config_dir)
    with pytest.raises(ap.ApplyError):
        ap.apply(cfg.profiles[0], cfg)


def test_disabled_profile_refuses_apply(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir, statusline={"enabled": False})
    with pytest.raises(ap.ApplyError) as err:
        ap.apply(cfg.profiles[0], cfg)
    assert err.value.code == "statusline_disabled"
    assert not (config_dir / "settings.json").exists()
    assert template.generate(cfg.profiles[0], cfg).path.is_file()  # generate still works


def test_reapply_with_other_interpreter_keeps_original_previous(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    data = json.loads(path.read_text())
    script = config_dir / "ccs-statusline.py"
    data["statusLine"]["command"] = template.statusline_command(script, "/old/venv/bin/python")
    path.write_text(json.dumps(data, indent=2))
    assert ap.is_ours(data["statusLine"], script)
    result = ap.apply(cfg.profiles[0], cfg)
    assert result.result == ap.APPLIED
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["previous_statusline"] == OLD_STATUSLINE
    assert ap.revert(cfg.profiles[0]).restored == OLD_STATUSLINE


def test_is_ours() -> None:
    script = Path("/x/ccs-statusline.py")
    assert ap.is_ours({"type": "command", "command": f"/py -S -E {script}"}, script)
    assert not ap.is_ours({"type": "command", "command": f"/py {script}"}, script)
    assert not ap.is_ours({"type": "command", "command": "bash other.sh"}, script)
    assert not ap.is_ours({"type": "static"}, script)
    assert not ap.is_ours(None, script)
    assert not ap.is_ours({"type": "command", "command": "unbalanced 'quote"}, script)


def test_ensure_statusline_launcher_hook(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    command = ap.ensure_statusline(cfg.profiles[0], cfg)
    script = config_dir / "ccs-statusline.py"
    assert command == template.statusline_command(script)
    assert script.is_file()
    assert ap.ensure_script is ap.ensure_statusline
    assert not (config_dir / "settings.json").exists()  # the hook never touches settings


def test_ensure_statusline_loads_config_and_handles_disabled(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    write_config(cfg)
    expected = template.statusline_command(config_dir / "ccs-statusline.py")
    assert ap.ensure_statusline(cfg.profiles[0]) == expected
    off = make_config(config_dir, statusline={"enabled": False})
    assert ap.ensure_statusline(off.profiles[0], off) is None


def test_ensure_statusline_failure_returns_none(tmp_path: Path) -> None:
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    cfg = make_config(blocked)
    assert ap.ensure_statusline(cfg.profiles[0], cfg) is None
