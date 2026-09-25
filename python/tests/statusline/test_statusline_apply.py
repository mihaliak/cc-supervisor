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


# ---------------------------------------------------------------- P15 review fixes


def test_revert_uses_the_recorded_settings_path(tmp_path: Path) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = write_settings(old_dir, existing_settings())
    cfg = make_config(old_dir)
    ap.apply(cfg.profiles[0], cfg)
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    moved = make_config(new_dir)  # config_dir changed after apply
    result = ap.revert(moved.profiles[0])
    assert result.result == ap.REVERTED
    assert json.loads(old.read_text()) == existing_settings()
    assert not (new_dir / "settings.json").exists()
    assert not paths.statusline_file("work").exists()


def test_apply_on_new_dir_reverts_the_old_one_first(tmp_path: Path) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = write_settings(old_dir, existing_settings())
    cfg = make_config(old_dir)
    ap.apply(cfg.profiles[0], cfg)
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    write_settings(new_dir, {"theme": "light"})
    moved = make_config(new_dir)
    result = ap.apply(moved.profiles[0], moved)
    assert result.result == ap.APPLIED
    assert json.loads(old.read_text()) == existing_settings()  # the original is back
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["settings_path"] == str(new_dir / "settings.json")
    assert book["previous_statusline"] is None
    assert ap.revert(moved.profiles[0]).result == ap.REVERTED
    assert json.loads((new_dir / "settings.json").read_text()) == {"theme": "light"}


def test_apply_after_the_dir_moved_keeps_the_original_previous(tmp_path: Path) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    write_settings(old_dir, existing_settings())
    cfg = make_config(old_dir)
    ap.apply(cfg.profiles[0], cfg)
    new_dir = tmp_path / "new"
    old_dir.rename(new_dir)  # `mv ~/.claude-a ~/.claude-b`, then config_dir updated
    moved = make_config(new_dir)
    assert ap.apply(moved.profiles[0], moved).result == ap.APPLIED
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["previous_statusline"] == OLD_STATUSLINE
    ap.revert(moved.profiles[0])
    assert json.loads((new_dir / "settings.json").read_text()) == existing_settings()


def test_apply_refuses_while_the_old_settings_are_broken(tmp_path: Path) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    cfg = make_config(old_dir)
    ap.apply(cfg.profiles[0], cfg)
    (old_dir / "settings.json").write_text("{broken")
    new_dir = new_config_dir(tmp_path)
    moved = make_config(new_dir)
    with pytest.raises(ap.ApplyError) as err:
        ap.apply(moved.profiles[0], moved)
    assert "reverted first" in str(err.value)
    assert not (new_dir / "settings.json").exists()
    assert paths.statusline_file("work").exists()


def test_reapply_keeps_extra_keys_without_a_new_backup(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    data = json.loads(path.read_text())
    data["statusLine"]["padding"] = 0
    path.write_text(json.dumps(data, indent=2))
    backups = sorted(config_dir.glob("settings.json.ccs-backup-*"))
    before = path.read_text()
    assert ap.apply(cfg.profiles[0], cfg).result == ap.ALREADY_APPLIED
    assert path.read_text() == before
    assert sorted(config_dir.glob("settings.json.ccs-backup-*")) == backups


def test_replacing_carries_over_extra_keys(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    mine = {"type": "command", "command": "bash mine.sh", "padding": 2}
    path = write_settings(config_dir, {"statusLine": mine})
    cfg = make_config(config_dir)
    result = ap.apply(cfg.profiles[0], cfg)
    value = json.loads(path.read_text())["statusLine"]
    assert value == {"type": "command", "command": result.command, "padding": 2}
    assert list(value) == ["type", "command", "padding"]
    # ours with an old interpreter: the new command keeps the user's padding too
    value["command"] = template.statusline_command(config_dir / "ccs-statusline.py", "/old/py")
    path.write_text(json.dumps({"statusLine": value}))
    assert ap.apply(cfg.profiles[0], cfg).result == ap.APPLIED
    assert json.loads(path.read_text())["statusLine"]["padding"] == 2
    assert ap.revert(cfg.profiles[0]).restored == mine


def test_ours_without_bookkeeping_records_no_previous(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, {"theme": "dark"})
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    paths.statusline_file("work").unlink()
    data = json.loads(path.read_text())
    script = config_dir / "ccs-statusline.py"
    data["statusLine"]["command"] = template.statusline_command(script, "/usr/bin/python3")
    path.write_text(json.dumps(data))
    assert ap.apply(cfg.profiles[0], cfg).result == ap.APPLIED
    assert json.loads(paths.statusline_file("work").read_text())["previous_statusline"] is None
    assert ap.revert(cfg.profiles[0]).result == ap.REVERTED
    assert json.loads(path.read_text()) == {"theme": "dark"}  # not our old command


def test_already_applied_without_bookkeeping_records_it(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, {"theme": "dark"})
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    paths.statusline_file("work").unlink()
    assert ap.apply(cfg.profiles[0], cfg).result == ap.ALREADY_APPLIED
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["previous_statusline"] is None and book["backup_path"] is None
    assert ap.revert(cfg.profiles[0]).result == ap.REVERTED
    assert json.loads(path.read_text()) == {"theme": "dark"}


def test_bookkeeping_failure_rolls_settings_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)

    def full_disk(*_: Any, **__: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ap, "atomic_write_json", full_disk)
    with pytest.raises(OSError):
        ap.apply(cfg.profiles[0], cfg)
    assert json.loads(path.read_text()) == existing_settings()
    assert not paths.statusline_file("work").exists()
    fresh = tmp_path / "fresh"  # no settings.json yet: the new file is removed again
    fresh.mkdir()
    cfg = make_config(fresh)
    with pytest.raises(OSError):
        ap.apply(cfg.profiles[0], cfg)
    assert not (fresh / "settings.json").exists()


def test_unwritable_settings_write_nothing(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    path = config_dir / "settings.json"
    path.write_text('{"x": "\\ud83d"}')  # a lone surrogate: can't be written back as UTF-8
    cfg = make_config(config_dir)
    with pytest.raises(ap.ApplyError) as err:
        ap.apply(cfg.profiles[0], cfg)
    assert err.value.code == "invalid_settings"
    assert path.read_text() == '{"x": "\\ud83d"}'
    assert not paths.statusline_file("work").exists()
    assert list(config_dir.glob("settings.json.ccs-backup-*")) == []


# --- concurrent writers (Claude Code rewrites settings.json itself; no shared lock) ----------


def _race(monkeypatch: pytest.MonkeyPatch, path: Path, times: int = 1) -> list[dict[str, Any]]:
    """Make "Claude Code" rewrite `path` right after ccs computed its new text (`times` times)."""
    real = ap._settings_text
    writes: list[dict[str, Any]] = []

    def racing(*args: Any, **kwargs: Any) -> str:
        text = real(*args, **kwargs)
        if len(writes) < times:
            data = json.loads(path.read_text())
            data["claudeCodeWrite"] = len(writes) + 1
            path.write_text(json.dumps(data, indent=2) + "\n")
            writes.append(data)
        return text

    monkeypatch.setattr(ap, "_settings_text", racing)
    return writes


def test_apply_keeps_a_concurrent_claude_code_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    writes = _race(monkeypatch, path)
    result = ap.apply(cfg.profiles[0], cfg)
    assert result.result == ap.APPLIED and len(writes) == 1
    data = json.loads(path.read_text())
    assert data["claudeCodeWrite"] == 1  # not silently discarded
    assert data["statusLine"]["command"] == result.command
    assert result.backup_path is not None
    assert json.loads(result.backup_path.read_text())["claudeCodeWrite"] == 1
    assert len(list(config_dir.glob("settings.json.ccs-backup-*"))) == 1
    book = json.loads(paths.statusline_file("work").read_text())
    assert book["previous_statusline"] == OLD_STATUSLINE


def test_apply_gives_up_when_settings_keep_changing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    _race(monkeypatch, path, times=100)
    with pytest.raises(ap.ApplyError) as err:
        ap.apply(cfg.profiles[0], cfg)
    assert err.value.code == "settings_busy"
    assert "try again" in str(err.value)
    assert json.loads(path.read_text())["statusLine"] == OLD_STATUSLINE  # theirs, untouched
    assert list(config_dir.glob("settings.json.ccs-backup-*")) == []
    assert not paths.statusline_file("work").exists()


def test_revert_keeps_a_concurrent_claude_code_write_and_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    backups = set(config_dir.glob("settings.json.ccs-backup-*"))
    writes = _race(monkeypatch, path)
    result = ap.revert(cfg.profiles[0])
    assert result.result == ap.REVERTED and len(writes) == 1
    data = json.loads(path.read_text())
    assert data["claudeCodeWrite"] == 1
    assert data["statusLine"] == OLD_STATUSLINE
    (backup,) = set(config_dir.glob("settings.json.ccs-backup-*")) - backups
    assert result.backup_path == backup
    assert result.to_dict()["backup_path"] == str(backup)
    assert json.loads(backup.read_text()) == writes[0]  # what revert replaced
    assert not paths.statusline_file("work").exists()


def test_revert_gives_up_when_settings_keep_changing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = new_config_dir(tmp_path)
    path = write_settings(config_dir, existing_settings())
    cfg = make_config(config_dir)
    ap.apply(cfg.profiles[0], cfg)
    backups = set(config_dir.glob("settings.json.ccs-backup-*"))
    _race(monkeypatch, path, times=100)
    with pytest.raises(ap.ApplyError) as err:
        ap.revert(cfg.profiles[0])
    assert err.value.code == "settings_busy"
    assert ap.is_ours(json.loads(path.read_text())["statusLine"], config_dir / "ccs-statusline.py")
    assert set(config_dir.glob("settings.json.ccs-backup-*")) == backups
    assert paths.statusline_file("work").exists()  # still revertable later


# --- the old config dir's record must survive a conflicted apply ---------------------------


@pytest.mark.parametrize("new_has_ours", [False, True])
def test_apply_refuses_to_drop_the_old_dirs_record_on_conflict(
    tmp_path: Path, new_has_ours: bool
) -> None:
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = write_settings(old_dir, existing_settings())
    cfg = make_config(old_dir)
    ap.apply(cfg.profiles[0], cfg)
    mine = {"type": "command", "command": "echo mine"}
    data = json.loads(old.read_text())
    data["statusLine"] = mine  # the user changed the old dir's statusLine by hand
    old.write_text(json.dumps(data))
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    moved = make_config(new_dir)
    new_settings: dict[str, Any] = {"theme": "light"}
    if new_has_ours:
        new_settings["statusLine"] = template.statusline_setting(new_dir / "ccs-statusline.py")
    new = write_settings(new_dir, new_settings)
    book_before = paths.statusline_file("work").read_text()
    with pytest.raises(ap.ApplyError) as err:
        ap.apply(moved.profiles[0], moved)
    assert err.value.code == "conflict"
    message = str(err.value)
    assert str(old) in message and str(paths.statusline_file("work")) in message
    assert paths.statusline_file("work").read_text() == book_before  # record kept
    assert json.loads(new.read_text()) == new_settings
    assert json.loads(old.read_text())["statusLine"] == mine
    assert list(new_dir.glob("settings.json.ccs-backup-*")) == []


def test_cli_apply_reports_the_old_dirs_conflict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ccs.cli import main

    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = write_settings(old_dir, existing_settings())
    cfg = make_config(old_dir)
    ap.apply(cfg.profiles[0], cfg)
    old.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}))
    write_config(make_config(new_config_dir(tmp_path)))
    assert main(["statusline", "apply", "--profile", "work", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and str(old) in doc["error"]
    assert doc["issues"][0]["path"] == "conflict"
