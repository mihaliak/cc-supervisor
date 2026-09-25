"""`ccs.statusline.template`: the generated self-contained script."""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sl_helpers import load_fixture, make_config, new_config_dir, strip_ansi

from ccs import paths
from ccs.clock import local_tz
from ccs.statusline import template
from ccs.statusline.render import RenderContext, render, to_plain


def run_script(
    script: Path, data: Any, state: Path, *, wrapper: str | None = "w1", raw: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    env["CCS_STATE_DIR"] = str(state)
    if wrapper:
        env["CCS_WRAPPER_ID"] = wrapper
    cmd = template.statusline_command(script)
    return subprocess.run(
        cmd,
        shell=True,
        input=raw if raw is not None else json.dumps(data),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_generate_writes_executable_script(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    profile = cfg.profiles[0]
    first = template.generate(profile, cfg)
    assert first.changed is True
    assert first.path == config_dir / "ccs-statusline.py"
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o755
    text = first.path.read_text()
    assert text.startswith(f"#!{sys.executable} -S -E\n")
    header = template.parse_header(text)
    assert header is not None
    assert header["generator"] == str(template.GENERATOR_VERSION)
    assert header["profile"] == "work"
    assert header["sources"] == first.sources_hash
    assert first.to_dict()["script_path"] == str(first.path)
    second = template.generate(profile, cfg)
    assert second.changed is False
    assert template.script_is_current(profile, cfg)


def test_command_is_exact(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    script = config_dir / "ccs-statusline.py"
    assert template.statusline_command(script) == f"{sys.executable} -S -E {script}"
    spaced = config_dir / "with space" / "ccs-statusline.py"
    assert template.statusline_command(spaced, "/usr/bin/python3").endswith(f"'{spaced}'")
    assert template.statusline_setting(script)["type"] == "command"


def test_constants_are_baked(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    constants = template.constants_for(cfg.profiles[0], cfg)
    assert constants["profile"] == {"id": "work", "name": "Work", "emoji": "💼"}
    assert constants["colors"] == {"yellow_from": 50, "red_from": 80}
    assert constants["limits"]["session"] == {"warn": 80, "pause": 90}
    assert constants["state_dir"] == str(paths.state_dir())


def test_ensure_script_regenerates_on_any_difference(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    path = template.ensure_script(cfg.profiles[0], cfg)
    path.write_text(path.read_text() + "# tampered\n")
    assert not template.script_is_current(cfg.profiles[0], cfg)
    template.ensure_script(cfg.profiles[0], cfg)
    assert template.script_is_current(cfg.profiles[0], cfg)
    renamed = make_config(config_dir, name="Job")
    assert not template.script_is_current(renamed.profiles[0], renamed)
    assert '"name": "Job"' in template.ensure_script(renamed.profiles[0], renamed).read_text()


def test_script_runs_standalone_and_matches_render(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    script = template.ensure_script(cfg.profiles[0], cfg)
    state = tmp_path / "state"
    data = load_fixture("with_rate_limits.json")
    # No reset time → output independent of the wall clock.
    data["rate_limits"]["five_hour"].pop("resets_at")
    data["rate_limits"]["seven_day"].pop("resets_at")
    proc = run_script(script, data, state)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    context = RenderContext.from_constants(
        template.constants_for(cfg.profiles[0], cfg),
        now=datetime.now(UTC),
        tz=local_tz(),
        stdin=data,
        wrapper_id="w1",
        supervisor_offline=True,
    )
    assert strip_ansi(proc.stdout.rstrip("\n")) == to_plain(render(context))
    assert "\x1b[32m18% ▓▓░░░░░░░░\x1b[0m" in proc.stdout
    assert (state / "live" / "w1.json").is_file()


@pytest.mark.parametrize("raw", ["", "garbage", "[]"])
def test_script_never_fails(tmp_path: Path, raw: str) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    script = template.ensure_script(cfg.profiles[0], cfg)
    proc = run_script(script, None, tmp_path / "state", raw=raw)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert strip_ansi(proc.stdout).startswith("💼 Work ~ ?%")


def test_script_ignores_python_env_and_site(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    script = template.ensure_script(cfg.profiles[0], cfg)
    poison = tmp_path / "poison"
    (poison / "ccs").mkdir(parents=True)
    (poison / "ccs" / "__init__.py").write_text("raise SystemExit('poisoned')\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(poison)}
    env["CCS_STATE_DIR"] = str(tmp_path / "state")
    proc = subprocess.run(
        template.statusline_command(script),
        shell=True,
        input=json.dumps(load_fixture("with_effort.json")),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "Sonnet 5 / low" in proc.stdout


def test_config_dir_newline_cannot_escape_the_comment(tmp_path: Path) -> None:
    config_dir = tmp_path / 'x\nprint("INJECTED")\n#\x85'
    config_dir.mkdir()
    cfg = make_config(config_dir)
    script = template.generate(cfg.profiles[0], cfg).path
    text = script.read_text()
    run_as = [line for line in text.splitlines() if line.startswith("# Run as: ")]
    assert len(run_as) == 1 and '\\nprint("INJECTED")\\n#\\x85' in run_as[0]
    assert "\nprint(" not in text.split("\n_CONSTANTS_JSON = ", 1)[0]
    proc = subprocess.run(
        [sys.executable, "-S", "-E", str(script)],
        input="{}",
        capture_output=True,
        text=True,
        env={"CCS_STATE_DIR": str(tmp_path / "state")},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert strip_ansi(proc.stdout).startswith("💼 Work ~ ?%")
    assert "INJECTED" not in proc.stdout


def test_embedded_modules_import_only_stdlib() -> None:
    allowed = set(template.EMBEDDED_MODULES) | {"ccs", "ccs.statusline"}
    for name, source in template.embedded_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{name}: relative import"
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if module == "__future__":
                    continue
                top = module.split(".")[0]
                ok = module in allowed or (top != "ccs" and top in sys.stdlib_module_names)
                assert ok, f"{name} imports {module}"


def test_embedded_dependency_order() -> None:
    order = list(template.EMBEDDED_MODULES)
    for name, source in template.embedded_sources().items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ccs."):
                assert order.index(node.module or "") < order.index(name)


def test_parse_header_rejects_foreign_text() -> None:
    assert template.parse_header("#!/bin/bash\necho hi\n") is None


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_non_finite_constant_cannot_kill_the_script(tmp_path: Path, bad: float) -> None:
    # `validate` rejects these, but a baked `inf`/`nan` must never become a NameError
    config_dir = new_config_dir(tmp_path)
    limits = {"session": {"warn": 80, "pause": 90, "note": bad}}
    cfg = make_config(config_dir, limits=limits)
    script = template.generate(cfg.profiles[0], cfg).path
    proc = run_script(script, load_fixture("with_effort.json"), tmp_path / "state")
    assert proc.returncode == 0, proc.stderr
    assert "Error" not in proc.stderr
    assert strip_ansi(proc.stdout).startswith("💼 Work ~ ")
