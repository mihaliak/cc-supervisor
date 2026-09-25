"""Warm-up runner against the fake claude: success, failures, lock, env (P08)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from warmup_helpers import profile, snapshot

from ccs import claude_cli, paths
from ccs.clock import SystemClock
from ccs.events import Event
from ccs.usage.model import UsageSnapshot
from ccs.warmup import rules
from ccs.warmup.runner import (
    CLAUDE_NOT_FOUND,
    TIMEOUT,
    WINDOW_NOT_STARTED,
    WarmupRunner,
    build_command,
    stderr_tail,
    warmup_env,
)
from ccs.warmup.state import WarmupStore


def make_runner(
    fake_path: Path,
    poll_result: UsageSnapshot | None,
    *,
    timeout_s: float = 10.0,
    resolve: Callable[[], str] | None = None,
    confirm_delays: tuple[float, ...] = (0.0, 0.0),
    polls: list[UsageSnapshot | None] | None = None,
) -> tuple[WarmupRunner, list[Event]]:
    events: list[Event] = []

    async def poll(pid: str) -> UsageSnapshot | None:
        return polls.pop(0) if polls else poll_result

    runner = WarmupRunner(
        clock=SystemClock(),
        store=WarmupStore(),
        emit=events.append,
        poll=poll,
        resolve_claude=resolve or (lambda: str(fake_path)),
        timeout_s=timeout_s,
        confirm_delays=confirm_delays,
    )
    return runner, events


def active_window() -> UsageSnapshot:
    return snapshot(resets_at=SystemClock().now() + timedelta(hours=5))


def types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def test_build_command_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    prof = profile(model="haiku", prompt="Reply with just: ok")
    assert build_command(prof, "/bin/claude") == [
        "/bin/claude",
        "-p",
        "Reply with just: ok",
        "--model",
        "haiku",
        "--no-session-persistence",
        "--settings",
        '{"disableAllHooks":true}',
    ]
    monkeypatch.setenv("CCS_STATE_DIR", "/tmp/x")
    monkeypatch.setenv("CCS_PROFILE", "work")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = warmup_env(prof)
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/ccs-test-work"
    assert not [k for k in env if k.startswith("CCS_")]
    assert "CLAUDECODE" not in env


def test_stderr_tail() -> None:
    assert stderr_tail("  boom \n") == "boom"
    long = "x" * 600 + "END"
    tail = stderr_tail(long)
    assert len(tail) == 500 and tail.endswith("END") and tail.startswith("…")


def test_success(fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path) -> None:
    fake = fake_claude({"print": {"stdout": "ok"}})
    runner, events = make_runner(fake_claude_path, active_window())
    outcome = asyncio.run(runner.run(profile(), rules.MANUAL))
    assert outcome.result == "succeeded" and outcome.reason is None and outcome.rc == 0
    assert types(events) == ["warmup.started", "warmup.succeeded"]
    done = events[1]
    assert done.data["resets_at"] and done.data["trigger"] == "manual"
    assert done.key and done.key.startswith("warmup:work:")
    call = fake.calls()[-1]
    assert call["mode"] == "print"
    assert call["argv"][:2] == ["-p", "Reply with just: ok"]
    assert call["env"]["CLAUDE_CONFIG_DIR"] == "/tmp/ccs-test-work"
    assert not [k for k in call["env"] if k.startswith("CCS_")]
    assert Path(call["cwd"]).resolve() == paths.warmup_cwd().resolve()
    doc = runner.store.view("work")
    assert doc["last_attempt"]["result"] == "succeeded"
    assert doc["last_success_at"] and len(doc["history"]) == 1
    assert paths.warmup_file("work").exists()


def test_nonzero_exit(fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path) -> None:
    fake_claude({"print": {"exit": 3, "stderr": "boom: rate limited"}})
    runner, events = make_runner(fake_claude_path, active_window())
    outcome = asyncio.run(runner.run(profile(), rules.SCHEDULE))
    assert outcome.result == "failed" and outcome.reason == "exit_3"
    assert outcome.detail == "boom: rate limited"
    assert events[-1].type == "warmup.failed"
    assert events[-1].data == {
        "trigger": "schedule",
        "reason": "exit_3",
        "detail": "boom: rate limited",
    }


def test_timeout_kills(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    fake_claude({"print": {"delay": 30}})
    runner, events = make_runner(fake_claude_path, active_window(), timeout_s=0.5)
    outcome = asyncio.run(runner.run(profile(), rules.MANUAL))
    assert outcome.reason == TIMEOUT
    assert events[-1].type == "warmup.failed"


def test_window_not_started(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    fake_claude({"print": {"stdout": "ok"}})
    runner, _ = make_runner(fake_claude_path, snapshot(session=False))
    outcome = asyncio.run(runner.run(profile(), rules.AUTO_CHAIN))
    assert outcome.reason == WINDOW_NOT_STARTED
    assert runner.store.view("work")["last_attempt"]["reason"] == WINDOW_NOT_STARTED


def test_window_that_shows_up_late_counts(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    # the usage API lagged the warm-up request: the first polls still show no window
    fake_claude({"print": {"stdout": "ok"}})
    late = [snapshot(session=False), snapshot(session=False)]
    runner, events = make_runner(fake_claude_path, active_window(), polls=late)
    outcome = asyncio.run(runner.run(profile(), rules.AUTO_CHAIN))
    assert outcome.reason is None
    assert outcome.resets_at is not None
    assert types(events) == ["warmup.started", "warmup.succeeded"]


def test_window_confirmation_polls_once_per_delay(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    fake_claude({"print": {"stdout": "ok"}})
    calls: list[str] = []
    runner, _ = make_runner(fake_claude_path, snapshot(session=False), confirm_delays=(0.0,) * 3)
    inner = runner._poll

    async def counting(pid: str) -> UsageSnapshot | None:
        calls.append(pid)
        return await inner(pid)

    runner._poll = counting
    outcome = asyncio.run(runner.run(profile(), rules.AUTO_CHAIN))
    assert outcome.reason == WINDOW_NOT_STARTED
    assert calls == ["work"] * 4  # right away, then after each delay


def test_claude_not_found(fake_claude_path: Path) -> None:
    def missing() -> str:
        raise claude_cli.ClaudeNotFound("claude not found on PATH")

    runner, _ = make_runner(fake_claude_path, None, resolve=missing)
    outcome = asyncio.run(runner.run(profile(), rules.MANUAL))
    assert outcome.reason == CLAUDE_NOT_FOUND and outcome.detail


def test_concurrent_run_is_in_progress(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    fake_claude({"print": {"delay": 0.5}})
    runner, events = make_runner(fake_claude_path, active_window())

    async def body() -> list[str]:
        first = asyncio.ensure_future(runner.run(profile(), rules.MANUAL))
        await asyncio.sleep(0.05)
        assert runner.is_running("work")
        second = await runner.run(profile(), rules.APP_START)
        assert second.result == "skipped" and second.reason == rules.IN_PROGRESS
        return [(await first).result, second.result]

    assert asyncio.run(body()) == ["succeeded", "skipped"]
    assert types(events).count("warmup.skipped") == 1
    doc = runner.store.view("work")
    assert doc["last_skip"]["reason"] == rules.IN_PROGRESS
    assert len(doc["history"]) == 1  # skips are not attempts


def test_back_to_back_attempts_get_distinct_event_keys(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    fake_claude({"print": {"stdout": "ok"}})
    runner, events = make_runner(fake_claude_path, active_window())

    async def body() -> None:
        await runner.run(profile(), rules.MANUAL)
        await runner.run(profile(), rules.MANUAL)

    asyncio.run(body())
    keys = [e.key for e in events if e.type == "warmup.succeeded"]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_cooldown_starts_at_attempt_begin(
    fake_claude: Callable[[dict[str, Any]], Any], fake_claude_path: Path
) -> None:
    fake_claude({"print": {"stdout": "ok"}})
    runner, _ = make_runner(fake_claude_path, active_window())
    asyncio.run(runner.run(profile(), rules.MANUAL))
    last = runner.store.last_attempt_at("work")
    assert last is not None
    # a fresh store reads the persisted attempt (cooldown survives restarts)
    assert WarmupStore().last_attempt_at("work") == last
