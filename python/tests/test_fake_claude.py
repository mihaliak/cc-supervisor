from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from conftest import FakeClaude


def run(fc: FakeClaude, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(fc.path), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=fc.env,
        timeout=10,
        check=False,
    )


def test_version_scenario_and_call_log(
    fake_claude: Callable[[dict[str, Any]], FakeClaude],
) -> None:
    fc = fake_claude({"version": {"stdout": "9.9.9 (Claude Code)"}})
    fc.env["CLAUDE_CONFIG_DIR"] = "/tmp/profile"
    result = run(fc, "--version")
    assert result.returncode == 0
    assert result.stdout.strip() == "9.9.9 (Claude Code)"
    calls = fc.calls()
    assert len(calls) == 1
    assert calls[0]["argv"] == ["--version"]
    assert calls[0]["mode"] == "version"
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] == "/tmp/profile"


def test_unknown_mode_exits_2(fake_claude: Callable[[dict[str, Any]], FakeClaude]) -> None:
    fc = fake_claude({})
    result = run(fc, "definitely-not-a-command")
    assert result.returncode == 2
    assert "unknown mode" in result.stderr


def test_agents_json(fake_claude: Callable[[dict[str, Any]], FakeClaude]) -> None:
    sessions = [{"pid": 42, "kind": "interactive", "status": "idle"}]
    fc = fake_claude({"agents": {"sessions": sessions}})
    result = run(fc, "agents", "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout) == sessions


def test_stream_get_usage(fake_claude: Callable[[dict[str, Any]], FakeClaude]) -> None:
    payload = {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 5}}}
    fc = fake_claude({"stream": {"get_usage": {"response": payload}}})
    request = {
        "type": "control_request",
        "request_id": "r1",
        "request": {"subtype": "get_usage", "skip_behaviors": True},
    }
    result = run(
        fc,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        stdin=json.dumps(request) + "\n",
    )
    assert result.returncode == 0
    lines = [json.loads(raw) for raw in result.stdout.splitlines()]
    # no --settings disableAllHooks → the fake emits hook lines first, like the real claude
    assert [m["subtype"] for m in lines if m["type"] == "system"] == ["hook_started"] * 3
    line = next(m for m in lines if m["type"] == "control_response")
    assert line["response"]["request_id"] == "r1"
    assert line["response"]["response"] == payload


def test_print_mode_default_ok(fake_claude: Callable[[dict[str, Any]], FakeClaude]) -> None:
    fc = fake_claude({})
    result = run(fc, "-p", "Reply with just: ok", "--model", "haiku")
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
