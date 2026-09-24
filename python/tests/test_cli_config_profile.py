from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import XdgDirs

from ccs import paths
from ccs.cli import main


@pytest.fixture
def env(tmp_xdg: XdgDirs, tmp_home: Path) -> Path:
    (tmp_home / ".claude").mkdir()
    (tmp_home / ".claude-work").mkdir()
    return tmp_home


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    doc = json.loads(out) if "--json" in argv and out.strip() else out
    return code, doc, err


def test_profile_list_seeds(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "profile", "list", "--json")
    assert code == 0 and doc["ok"] is True
    assert [p["id"] for p in doc["profiles"]] == ["personal", "work"]
    assert doc["default_profile"] == "personal"
    assert doc["profiles"][0]["default"] is True
    assert paths.config_file().exists()


def test_profile_add_set_remove(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(
        capsys,
        "profile",
        "add",
        "--id",
        "lab",
        "--emoji",
        "🧪",
        "--config-dir",
        "~/.claude-lab",
        "--json",
    )
    assert code == 0 and doc["profile"]["flag"] == "lab" and doc["profile"]["name"] == "Lab"

    code, doc, _ = run(
        capsys,
        "profile",
        "set",
        "lab",
        "limits.session.pause=92",
        "name=Lab Box",
        'warmup.triggers.schedule=[{"time":"06:00","weekdays":["mon"]}]',
        "--json",
    )
    assert code == 0
    code, doc, _ = run(capsys, "profile", "show", "lab", "--json")
    prof = doc["profile"]
    assert prof["limits"]["session"]["pause"] == 92
    assert prof["name"] == "Lab Box"
    assert prof["warmup"]["triggers"]["schedule"] == [{"time": "06:00", "weekdays": ["mon"]}]

    code, doc, _ = run(capsys, "profile", "remove", "lab", "--json")
    assert code == 0 and doc["removed"] == "lab"
    code, doc, _ = run(capsys, "profile", "list", "--json")
    assert "lab" not in [p["id"] for p in doc["profiles"]]


def test_profile_set_invalid_exits_1(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "profile", "set", "work", "limits.session.warn=95", "--json")
    assert code == 1 and doc["ok"] is False
    assert "profiles[1].limits.session.warn" in [i["path"] for i in doc["issues"]]


def test_profile_set_id_is_immutable(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "profile", "set", "work", "id=other", "--json")
    assert code == 2 and doc["ok"] is False


def test_profile_add_reserved_flag(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(
        capsys,
        "profile",
        "add",
        "--id",
        "x",
        "--flag",
        "resume",
        "--emoji",
        "x",
        "--config-dir",
        "~/.claude-x",
        "--json",
    )
    assert code == 1
    assert any("reserved" in i["message"] for i in doc["issues"])


def test_remove_default_needs_new_default(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "profile", "remove", "personal")
    assert code == 2 and "--default" in err
    code, doc, _ = run(capsys, "profile", "remove", "personal", "--default", "work", "--json")
    assert code == 0
    code, doc, _ = run(capsys, "profile", "list", "--json")
    assert doc["default_profile"] == "work"


def test_config_set_and_forbidden(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "profile", "list", "--json")
    rev0 = json.loads(paths.config_file().read_text())["revision"]
    code, doc, _ = run(
        capsys, "config", "set", "default_profile=work", "display.menu_bar=icon_only", "--json"
    )
    assert code == 0 and doc["revision"] == rev0 + 1
    raw = json.loads(paths.config_file().read_text())
    assert raw["default_profile"] == "work" and raw["display"]["menu_bar"] == "icon_only"
    code, doc, _ = run(capsys, "config", "set", "profiles=[]", "--json")
    assert code == 2
    code, _, _ = run(capsys, "config", "set", "revision=5")
    assert code == 2
    code, _, _ = run(capsys, "config", "set", "nokey")
    assert code == 2


def test_config_validate(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "config", "validate", "--json")
    assert code == 1 and doc["ok"] is False  # missing file
    run(capsys, "profile", "list", "--json")
    code, doc, _ = run(capsys, "config", "validate", "--json")
    assert code == 0 and doc["ok"] is True and doc["issues"] == []
    raw = json.loads(paths.config_file().read_text())
    raw["profiles"][0]["limits"] = {"weekly": {"warn": 99, "pause": 95}}
    paths.config_file().write_text(json.dumps(raw))
    code, doc, _ = run(capsys, "config", "validate", "--json")
    assert code == 1
    assert doc["issues"][0]["path"] == "profiles[0].limits.weekly.warn"
    paths.config_file().write_text("{broken")
    code, doc, _ = run(capsys, "config", "validate", "--json")
    assert code == 1 and doc["revision"] is None


def test_config_show_path_defaults(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "config", "path", "--json")
    assert code == 0 and doc["path"] == str(paths.config_file())
    code, doc, _ = run(capsys, "config", "show", "--json")
    assert code == 0 and doc["config"]["polling"]["interval_seconds"] == 60
    assert doc["config"]["profiles"][0]["warmup"]["model"] == "haiku"
    code, doc, _ = run(capsys, "config", "defaults", "--json")
    assert code == 0
    assert doc["config"]["display"]["colors"] == {"yellow_from": 50, "red_from": 80}
    assert doc["profile"]["limits"]["weekly"] == {"warn": 80, "pause": 95}


def test_human_output(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "profile", "list")
    assert code == 0 and "--work" in out and "*" in out
    code, out, _ = run(capsys, "config", "path")
    assert out.strip() == str(paths.config_file())


def test_unknown_profile(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "profile", "show", "nope", "--json")
    assert code == 1 and "nope" in doc["error"]
