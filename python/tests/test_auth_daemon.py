"""P09 integration: logged-out probe → `auth.required` → `ccs auth login` → next poll `ok`."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from daemon_helpers import ThreadedDaemon, scenario, short_state_dir, write_config

from ccs import paths
from ccs.cli import main


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_state_dir(monkeypatch) as d:
        yield d


def _usage_status(pid: str) -> str | None:
    try:
        data = json.loads(paths.usage_file(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = data.get("status")
    return status if isinstance(status, str) else None


def _events() -> list[dict[str, Any]]:
    try:
        lines = paths.events_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _wait(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


def test_login_clears_needs_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: Path,
) -> None:
    scenario(
        tmp_path,
        monkeypatch,
        {
            "stream": {"get_usage": {"mode": "auth_marker"}},
            "auth_status": {"stateful": True},
            "auth_login": {"sets_auth": True},
        },
    )
    write_config(tmp_path)
    paths.ensure_state_layout()
    with ThreadedDaemon():
        _wait(lambda: _usage_status("work") == "needs_sign_in")
        auth_events = [e for e in _events() if e["type"] == "auth.required"]
        assert len(auth_events) == 1 and auth_events[0]["profile_id"] == "work"

        code = main(["auth", "login", "--profile", "work", "--json"])
        doc = json.loads(capsys.readouterr().out)
        assert code == 0
        assert doc["logged_in"] is True and doc["daemon_refreshed"] is True

        _wait(lambda: _usage_status("work") == "ok")
    usage = json.loads(paths.usage_file("work").read_text(encoding="utf-8"))
    assert usage["status"] == "ok" and usage["error"] is None
