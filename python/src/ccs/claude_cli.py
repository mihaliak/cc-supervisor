"""Thin async wrappers around the `claude` executable (ADR-0002, ADR-0003, ADR-0013).

Every call runs with a profile env (`profile_env`): `CLAUDE_CONFIG_DIR` set and the parent
Claude Code session's variables stripped. The `get_usage` probe exits gracefully (stdin EOF,
then wait) because a SIGKILLed probe leaves Claude's pid registry files behind (P00-S2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccs import paths
from ccs.config.models import Config, Profile

log = logging.getLogger(__name__)

DISABLE_HOOKS_SETTINGS = '{"disableAllHooks":true}'
STDERR_LIMIT = 64 * 1024
STREAM_LINE_LIMIT = 16 * 1024 * 1024
GRACEFUL_EXIT_S = 5.0
TERM_GRACE_S = 2.0

# Parent-session variables that must not leak into probes/warm-ups (P03 risk list, P00-S2).
_STRIP_EXACT = frozenset(
    {"CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_PID", "AI_AGENT", "CCS_WRAPPER_ID", "CCS_PROFILE"}
)
_STRIP_PREFIXES = ("CLAUDE_CODE_",)


# pids of `claude` children spawned by this process (running or recently exited). The daemon
# excludes them from `claude agents --json` (a probe shows up there as an idle interactive
# session for ~1.5 s, P00-S2).
_RUNNING: set[int] = set()
_EXITED: dict[int, float] = {}
RECENT_EXIT_S = 10.0


def _track_spawn(pid: int) -> None:
    _RUNNING.add(pid)


def _track_exit(pid: int) -> None:
    _RUNNING.discard(pid)
    now = time.monotonic()
    _EXITED[pid] = now
    for old, when in list(_EXITED.items()):
        if now - when > RECENT_EXIT_S:
            del _EXITED[old]


def spawned_pids(recent_s: float = RECENT_EXIT_S) -> set[int]:
    """Pids of `claude` children still running or exited within `recent_s` seconds."""
    now = time.monotonic()
    return set(_RUNNING) | {pid for pid, when in _EXITED.items() if now - when <= recent_s}


class ClaudeCliError(Exception):
    """A `claude` invocation failed (bad exit, unparseable output)."""


class ClaudeNotFound(ClaudeCliError):
    """No `claude` executable could be located."""


class ClaudeTimeout(ClaudeCliError):
    """A `claude` invocation exceeded its deadline (the process was killed)."""


@dataclass(frozen=True)
class Completed:
    """Result of `run`."""

    rc: int
    stdout: str
    stderr: str
    duration: float


@dataclass(frozen=True)
class ProbeResult:
    """Result of `probe_usage`.

    `raw` is the control response payload (`response.response`) on success. Otherwise
    `error_kind` is `source_error` and `error` explains why.
    """

    raw: dict[str, Any] | None
    error_kind: str | None
    error: str | None
    duration: float
    rc: int | None = None
    stderr: str = ""
    forced_kill: bool = False
    pid: int | None = None


def resolve_claude(cfg: Config | None = None) -> str:
    """`cfg.claude_path`, else `claude` on PATH, else `~/.local/bin/claude`."""
    if cfg is not None and cfg.claude_path:
        path = os.path.expanduser(cfg.claude_path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise ClaudeNotFound(f"claude_path does not point to an executable: {cfg.claude_path}")
    found = shutil.which("claude")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/claude")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    raise ClaudeNotFound("claude not found on PATH (set claude_path in the config)")


def profile_env(profile: Profile, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment for `profile`: `CLAUDE_CONFIG_DIR` set, parent session stripped."""
    env = {
        k: v
        for k, v in (os.environ if base is None else base).items()
        if k not in _STRIP_EXACT and not k.startswith(_STRIP_PREFIXES)
    }
    env["CLAUDE_CONFIG_DIR"] = profile.config_dir_env
    return env


def _default_cwd() -> str:
    """A neutral, empty working dir for probes (the warm-up cwd, ADR-0005)."""
    cwd = paths.warmup_cwd()
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        return os.path.expanduser("~")
    return str(cwd)


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(sig)


async def _reap(proc: asyncio.subprocess.Process, *, graceful_s: float) -> bool:
    """Wait for exit; escalate SIGTERM → SIGKILL on the process group. True if forced."""
    try:
        await asyncio.wait_for(proc.wait(), graceful_s)
        return False
    except TimeoutError:
        pass
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), TERM_GRACE_S)
    except TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        await proc.wait()
    return True


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> str:
    """Read a stream to EOF keeping at most `limit` bytes (so the child never blocks)."""
    if stream is None:
        return ""
    kept = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if len(kept) < limit:
            kept += chunk[: limit - len(kept)]
    return kept.decode("utf-8", errors="replace")


async def run(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str | None = None,
    timeout: float,
    stdin_data: bytes | None = None,
) -> Completed:
    """Run a command to completion. On timeout the process group is killed → `ClaudeTimeout`."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd or _default_cwd(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ClaudeNotFound(f"cannot execute {argv[0]}: {exc}") from exc
    _track_spawn(proc.pid)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_data), timeout)
    except TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise ClaudeTimeout(f"{Path(argv[0]).name} {' '.join(argv[1:3])} timed out") from None
    except asyncio.CancelledError:
        _signal_group(proc, signal.SIGKILL)  # never leave a child behind a cancelled caller
        with contextlib.suppress(Exception):
            await asyncio.shield(proc.wait())
        raise
    finally:
        _track_exit(proc.pid)
    return Completed(
        rc=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace")[:STDERR_LIMIT],
        duration=time.monotonic() - started,
    )


def _control_request(request_id: str) -> bytes:
    msg = {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "get_usage", "skip_behaviors": True},
    }
    return (json.dumps(msg) + "\n").encode("utf-8")


def _match_response(line: bytes, request_id: str) -> dict[str, Any] | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "control_response":
        return None
    response = msg.get("response")
    if not isinstance(response, dict) or response.get("request_id") != request_id:
        return None
    return response


def remove_registry_files(config_dir: str, pid: int) -> list[Path]:
    """Delete exactly `<config_dir>/sessions/<pid>.json` and `<pid>.*.key` (killed probes only)."""
    sessions = Path(config_dir) / "sessions"
    removed: list[Path] = []
    candidates = [sessions / f"{pid}.json", *sessions.glob(f"{pid}.*.key")]
    for path in candidates:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed.append(path)
    return removed


async def probe_usage(
    claude: str,
    profile: Profile,
    *,
    timeout: float = 15.0,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    graceful_s: float = GRACEFUL_EXIT_S,
) -> ProbeResult:
    """Ask Claude Code for the profile's usage via the `get_usage` control request (ADR-0002)."""
    started = time.monotonic()
    request_id = f"ccs-{uuid.uuid4().hex[:12]}"
    argv = [
        claude,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--settings",
        DISABLE_HOOKS_SETTINGS,
    ]
    run_env = dict(env) if env is not None else profile_env(profile)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=run_env,
            cwd=cwd or _default_cwd(),
            start_new_session=True,
            limit=STREAM_LINE_LIMIT,
        )
    except OSError as exc:
        return ProbeResult(None, "source_error", f"cannot execute claude: {exc}", 0.0)
    _track_spawn(proc.pid)

    stderr_task = asyncio.ensure_future(_drain(proc.stderr, STDERR_LIMIT))
    raw: dict[str, Any] | None = None
    error: str | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(_control_request(request_id))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                error = f"no get_usage response within {timeout:g}s"
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except TimeoutError:
                error = f"no get_usage response within {timeout:g}s"
                break
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                error = "claude exited before answering get_usage"
                break
            response = _match_response(line, request_id)
            if response is None:
                continue
            if response.get("subtype") == "success":
                payload = response.get("response")
                if isinstance(payload, dict):
                    raw = payload
                else:
                    error = "get_usage response has no payload"
            else:
                detail = response.get("error")
                error = f"get_usage failed: {detail or response.get('subtype')}"
            break
    finally:
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            with contextlib.suppress(Exception):
                await proc.stdin.wait_closed()
        forced = await _reap(proc, graceful_s=graceful_s)
        _track_exit(proc.pid)
        if forced:
            removed = remove_registry_files(run_env.get("CLAUDE_CONFIG_DIR", ""), proc.pid)
            log.warning(
                "probe pid %s force-killed; removed %d registry files", proc.pid, len(removed)
            )
        stderr_text = await stderr_task

    rc = proc.returncode
    if raw is None and error == "claude exited before answering get_usage":
        tail = stderr_text.strip().splitlines()[-1:] if stderr_text.strip() else []
        error = f"claude exited (rc={rc}) before answering get_usage" + (
            f": {tail[0][:200]}" if tail else ""
        )
    return ProbeResult(
        raw=raw,
        error_kind=None if raw is not None else "source_error",
        error=None if raw is not None else error,
        duration=time.monotonic() - started,
        rc=rc,
        stderr=stderr_text,
        forced_kill=forced,
        pid=proc.pid,
    )


def _first_json(text: str) -> Any:
    """Decode the first JSON value in `text` (tolerates stray leading output)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                value, _ = decoder.raw_decode(text, i)
            except ValueError:
                continue
            return value
    raise ValueError("no JSON value in output")


async def agents_json(
    claude: str, profile: Profile, *, timeout: float = 10.0
) -> list[dict[str, Any]]:
    """`claude agents --json` for the profile (active interactive + background sessions)."""
    done = await run([claude, "agents", "--json"], env=profile_env(profile), timeout=timeout)
    if done.rc != 0:
        raise ClaudeCliError(f"claude agents --json exited {done.rc}: {done.stderr.strip()[:200]}")
    try:
        data = _first_json(done.stdout)
    except ValueError as exc:
        raise ClaudeCliError(f"claude agents --json: {exc}") from exc
    if not isinstance(data, list):
        raise ClaudeCliError("claude agents --json did not return a list")
    return [d for d in data if isinstance(d, dict)]


async def auth_status(
    claude: str, profile: Profile, *, timeout: float = 10.0
) -> dict[str, Any] | None:
    """`claude auth status --json` (rc 1 + JSON when logged out, P00-S4). `None` on failure."""
    try:
        done = await run(
            [claude, "auth", "status", "--json"], env=profile_env(profile), timeout=timeout
        )
    except ClaudeCliError as exc:
        log.info("auth status failed for %s: %s", profile.id, exc)
        return None
    try:
        data = _first_json(done.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


LOGIN_TIMEOUT_S = 600.0
LOGOUT_TIMEOUT_S = 30.0


async def auth_login(
    claude: str,
    profile: Profile,
    *,
    mode: str,
    timeout: float = LOGIN_TIMEOUT_S,
) -> Completed:
    """`claude auth login --claudeai` for the profile (ADR-0003, P00-S4).

    - `tty`: inherits this process's stdio and foreground process group, no timeout, so the
      user sees the URL and Ctrl-C works. `stdout`/`stderr` of the result are empty.
    - `headless`: stdin closed, output captured. Claude opens the browser itself and waits
      for the OAuth callback on localhost, so it completes without a terminal.
      Raises `ClaudeTimeout` after `timeout` seconds.

    `--claudeai` is always passed so no login-method prompt can appear.
    """
    argv = [claude, "auth", "login", "--claudeai"]
    env = profile_env(profile)
    if mode == "headless":
        return await run(argv, env=env, timeout=timeout)
    if mode != "tty":
        raise ValueError(f"unknown login mode: {mode}")
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(*argv, env=env)
    except OSError as exc:
        raise ClaudeNotFound(f"cannot execute {claude}: {exc}") from exc
    _track_spawn(proc.pid)
    try:
        rc = await proc.wait()
    finally:
        _track_exit(proc.pid)
    return Completed(rc=rc, stdout="", stderr="", duration=time.monotonic() - started)


async def auth_logout(
    claude: str, profile: Profile, *, timeout: float = LOGOUT_TIMEOUT_S
) -> Completed:
    """`claude auth logout` for the profile. Raises `ClaudeCliError` subclasses on failure."""
    return await run([claude, "auth", "logout"], env=profile_env(profile), timeout=timeout)
