"""`ccs auth login|status|logout`: Claude Code's own login per profile (ADR-0003).

CC Supervisor stores no secrets and never reads the Keychain. It runs `claude auth …` with the
profile's `CLAUDE_CONFIG_DIR` and only *computes* the Keychain service name Claude Code uses
(for `ccs doctor`). The account e-mail is shown to the user but never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ccs import paths
from ccs.claude_cli import (
    ClaudeCliError,
    ClaudeNotFound,
    ClaudeTimeout,
    auth_login,
    auth_logout,
    auth_status,
    resolve_claude,
)
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.daemon.client import DaemonClient, DaemonUnavailable
from ccs.output import EXIT_ERROR, EXIT_OK, EXIT_USAGE, UsageError, emit_json, eprint, fail

# P00-S4: `claude auth login --claudeai` completes with stdin=DEVNULL (it opens the browser and
# waits for the OAuth callback on localhost), so callers without a terminal sign in headless.
HEADLESS_LOGIN_SUPPORTED = True

LOGIN_TIMEOUT_S = 600.0
STATUS_TIMEOUT_S = 15.0
TERMINAL_POLL_S = 3.0
EXIT_CANCELLED = 130
DEFAULT_KEYCHAIN_SERVICE = "Claude Code-credentials"
# Carried into the Terminal window so its `ccs` sees the same config and state dirs.
ENV_PASSTHROUGH = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "CCS_STATE_DIR")

LoginMode = Literal["tty", "headless", "terminal"]
StatusCheck = Callable[[], Awaitable["AuthStatus | None"]]
Opener = Callable[[Path], int]


# ---------------------------------------------------------------- pure core


def keychain_service_name(config_dir: str | os.PathLike[str]) -> str:
    """The Keychain service Claude Code stores this config dir's login under (ADR-0003).

    `~/.claude` (or any path resolving to it) → `Claude Code-credentials`, because
    `CLAUDE_CONFIG_DIR` is unset for it (`paths.apply_claude_config_dir`). Any other dir →
    `Claude Code-credentials-<sha256(absolute dir)[:8]>`, with the path taken as Claude Code gets
    it in `CLAUDE_CONFIG_DIR` (`~` expanded, trailing slash dropped, symlinks kept). The Keychain
    is never read.
    """
    value = os.fspath(config_dir)
    if paths.is_default_claude_dir(value):
        return DEFAULT_KEYCHAIN_SERVICE
    given = paths.config_dir_env_value(value)
    digest = hashlib.sha256(given.encode("utf-8")).hexdigest()[:8]
    return f"{DEFAULT_KEYCHAIN_SERVICE}-{digest}"


@dataclass(frozen=True)
class AuthStatus:
    """Parsed `claude auth status --json` (P00-S4 fields). `account` is for display only."""

    logged_in: bool
    account: str | None = None
    subscription_type: str | None = None
    auth_method: str | None = None
    raw_keys: tuple[str, ...] = ()
    error: str | None = None


def _text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def parse_auth_status(raw: object) -> AuthStatus:
    """`claude auth status --json` → `AuthStatus`; unknown shapes → logged out + error."""
    if not isinstance(raw, dict):
        return AuthStatus(False, error="unrecognized `claude auth status` output")
    keys = tuple(sorted(str(k) for k in raw))
    logged_in = raw.get("loggedIn")
    if not isinstance(logged_in, bool):
        return AuthStatus(
            False, raw_keys=keys, error="unrecognized `claude auth status` output (no loggedIn)"
        )
    if not logged_in:
        return AuthStatus(False, auth_method=_text(raw, "authMethod"), raw_keys=keys)
    return AuthStatus(
        True,
        account=_text(raw, "email"),
        subscription_type=_text(raw, "subscriptionType"),
        auth_method=_text(raw, "authMethod"),
        raw_keys=keys,
    )


def choose_login_mode(
    *,
    stdin_tty: bool,
    stdout_tty: bool,
    terminal: bool,
    headless_supported: bool = HEADLESS_LOGIN_SUPPORTED,
) -> LoginMode:
    """`--terminal` wins; a full TTY signs in in place; otherwise headless (or Terminal)."""
    if terminal:
        return "terminal"
    if stdin_tty and stdout_tty:
        return "tty"
    return "headless" if headless_supported else "terminal"


def status_doc(profile: Profile, status: AuthStatus) -> dict[str, Any]:
    """The `ccs auth status --json` document (also used by `ccs doctor`)."""
    return {
        "ok": True,
        "profile_id": profile.id,
        "config_dir": profile.config_dir_env,
        "logged_in": status.logged_in,
        "account": status.account,
        "subscription_type": status.subscription_type,
        "auth_method": status.auth_method,
        "keychain_service": keychain_service_name(profile.config_dir),
        "error": status.error,
    }


def render_status(profile: Profile, status: AuthStatus) -> str:
    """Human `ccs auth status` block."""
    head = f"{profile.emoji} {profile.name} ({profile.config_dir})".strip()
    if status.logged_in:
        plan = f" · {status.subscription_type}" if status.subscription_type else ""
        who = status.account or "unknown account"
        line = f"signed in as {who}{plan}"
    elif status.error:
        line = f"⚠ {status.error}"
    else:
        line = f"not signed in · run: ccs auth login --profile {profile.id}"
    return "\n".join(
        [head, f"  {line}", f"  keychain item: {keychain_service_name(profile.config_dir)}"]
    )


# ---------------------------------------------------------------- Terminal mode


@dataclass(frozen=True)
class SigninFiles:
    """The `.command` script opened in Terminal and the file its exit code lands in."""

    command: Path
    done: Path


def signin_files(profile_id: str) -> SigninFiles:
    """`<state>/tmp/signin-<profile>.command` and `.done`."""
    base = paths.state_dir() / "tmp"
    return SigninFiles(base / f"signin-{profile_id}.command", base / f"signin-{profile_id}.done")


def ccs_argv(cfg: Config | None) -> list[str]:
    """How a fresh login shell runs this `ccs` (absolute paths)."""
    if cfg is not None and cfg.ccs_path:
        return [os.path.abspath(os.path.expanduser(cfg.ccs_path))]
    running = sys.argv[0] if sys.argv else ""
    if os.path.basename(running) == "ccs" and os.access(running, os.X_OK):
        return [os.path.abspath(running)]
    found = shutil.which("ccs")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "ccs"]


def command_file_text(
    argv: Sequence[str], profile_id: str, done: Path, env: Mapping[str, str]
) -> str:
    """A zsh `.command` script: `ccs auth login --profile <id>` (TTY mode), then its exit code."""
    lines = [
        "#!/bin/zsh -l",
        "# CC Supervisor: sign in to Claude Code. Deleted after use; holds no secrets.",
    ]
    for key in ENV_PASSTHROUGH:
        value = env.get(key)
        if value:
            lines.append(f"export {key}={shlex.quote(value)}")
    cmd = [*argv, "auth", "login", "--profile", profile_id]
    lines.append(" ".join(shlex.quote(a) for a in cmd))
    lines.append(f"print -r -- $? > {shlex.quote(str(done))}")
    return "\n".join(lines) + "\n"


def write_command_file(files: SigninFiles, text: str) -> None:
    """Write the script with mode 0700 (its dir 0700) and clear a stale `.done` file."""
    files.command.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(files.command.parent, 0o700)
    with contextlib.suppress(FileNotFoundError):
        files.done.unlink()
    tmp = files.command.with_name(files.command.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o700)
    os.replace(tmp, files.command)


def remove_signin_files(files: SigninFiles) -> None:
    for path in (files.command, files.done):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def open_in_terminal(path: Path) -> int:
    """`open -a Terminal <file>` (no Apple Events/Automation permission needed)."""
    done = subprocess.run(["open", "-a", "Terminal", str(path)], capture_output=True, check=False)
    return done.returncode


def read_done(files: SigninFiles) -> int | None:
    """The Terminal script's exit code, or `None` while it is still running."""
    try:
        text = files.done.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


async def wait_for_terminal_login(
    files: SigninFiles,
    check: StatusCheck,
    *,
    was_logged_in: bool,
    timeout: float = LOGIN_TIMEOUT_S,
    interval: float = TERMINAL_POLL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[AuthStatus | None, str | None]:
    """Wait for the Terminal sign-in; returns `(final status, error)`.

    Done when the script wrote its exit code, or (for a profile that was signed out) as soon
    as `auth status` shows it signed in. Gives up after `timeout` seconds.
    """
    deadline = monotonic() + timeout
    while True:
        rc = read_done(files)
        if rc is not None:
            status = await check()
            if status is not None and status.logged_in:
                return status, None
            suffix = f" (exit {rc})" if rc else ""
            return status, f"sign-in in Terminal did not complete{suffix}"
        if not was_logged_in:
            status = await check()
            if status is not None and status.logged_in:
                return status, None
        if monotonic() >= deadline:
            return await check(), f"timed out after {timeout:g} s waiting for the Terminal sign-in"
        await sleep(interval)


# ---------------------------------------------------------------- orchestration (IO)


async def fetch_status(
    claude: str, profile: Profile, *, timeout: float = STATUS_TIMEOUT_S
) -> AuthStatus | None:
    """`claude auth status --json`, parsed. `None` when claude could not be run or parsed."""
    raw = await auth_status(claude, profile, timeout=timeout)
    return None if raw is None else parse_auth_status(raw)


@dataclass(frozen=True)
class LoginOutcome:
    mode: LoginMode
    status: AuthStatus | None
    error: str | None

    @property
    def logged_in(self) -> bool:
        return self.status is not None and self.status.logged_in


def _tail(text: str) -> str | None:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else None


async def login(
    cfg: Config | None,
    profile: Profile,
    claude: str,
    mode: LoginMode,
    *,
    opener: Opener | None = None,
    env: Mapping[str, str] | None = None,
    ccs_cmd: Sequence[str] | None = None,
    timeout: float = LOGIN_TIMEOUT_S,
    poll_interval: float = TERMINAL_POLL_S,
    ui_to_stderr: bool = False,
) -> LoginOutcome:
    """Sign `profile` in with the given mode and report the resulting auth status.

    `ui_to_stderr`: in `tty` mode, claude's login UI goes to stderr (for `--json`).
    """
    if mode == "terminal":
        before = await fetch_status(claude, profile)
        files = signin_files(profile.id)
        argv = list(ccs_cmd) if ccs_cmd is not None else ccs_argv(cfg)
        write_command_file(
            files,
            command_file_text(argv, profile.id, files.done, os.environ if env is None else env),
        )
        try:
            if (opener or open_in_terminal)(files.command) != 0:
                return LoginOutcome(
                    mode, before, "could not open Terminal (open -a Terminal failed)"
                )
            status, error = await wait_for_terminal_login(
                files,
                lambda: fetch_status(claude, profile),
                was_logged_in=before is not None and before.logged_in,
                timeout=timeout,
                interval=poll_interval,
            )
            return LoginOutcome(mode, status, error)
        finally:
            remove_signin_files(files)
    try:
        done = await auth_login(
            claude, profile, mode=mode, timeout=timeout, stdout_to_stderr=ui_to_stderr
        )
    except ClaudeTimeout:
        return LoginOutcome(
            mode, await fetch_status(claude, profile), f"sign-in timed out after {timeout:g} s"
        )
    except ClaudeCliError as exc:
        return LoginOutcome(mode, None, str(exc))
    status = await fetch_status(claude, profile)
    if status is None:
        return LoginOutcome(mode, None, "could not read `claude auth status` after sign-in")
    if not status.logged_in:
        detail = _tail(done.stderr) or _tail(done.stdout)
        error = f"not signed in (claude auth login exited {done.rc})"
        return LoginOutcome(mode, status, f"{error}: {detail}" if detail else error)
    return LoginOutcome(mode, status, None)


async def logout(claude: str, profile: Profile) -> tuple[AuthStatus | None, str | None]:
    """`claude auth logout`, then the resulting status; returns `(status, error)`."""
    try:
        done = await auth_logout(claude, profile)
    except ClaudeCliError as exc:
        return None, str(exc)
    status = await fetch_status(claude, profile)
    if status is not None and status.logged_in:
        detail = _tail(done.stderr) or _tail(done.stdout)
        error = f"still signed in (claude auth logout exited {done.rc})"
        return status, f"{error}: {detail}" if detail else error
    return status, None


def request_daemon_refresh(profile_id: str, *, timeout: float = 2.0) -> bool:
    """Ask a running daemon to poll `profile_id` now; `False` when it isn't running."""
    try:
        with DaemonClient(timeout=timeout) as client:
            if not client.hello().get("ok"):
                return False
            return bool(client.request("refresh", profile_id=profile_id).get("ok"))
    except (DaemonUnavailable, OSError):
        return False


# ---------------------------------------------------------------- CLI


def _load_profile(profile_id: str) -> tuple[Config, Profile]:
    cfg = store.ensure_config()
    profile = cfg.profile(profile_id)
    if profile is None:
        raise UsageError(f"unknown profile '{profile_id}'")
    return cfg, profile


def _setup(args: argparse.Namespace) -> tuple[Config, Profile, str] | int:
    """Config, profile and `claude` path, or the exit code of the reported error."""
    as_json = bool(args.json)
    try:
        cfg, profile = _load_profile(args.profile)
        claude = resolve_claude(cfg)
    except UsageError as exc:
        return fail(as_json, str(exc), code=EXIT_USAGE)
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    except ClaudeNotFound as exc:
        return fail(as_json, str(exc))
    return cfg, profile, claude


def cmd_status(args: argparse.Namespace) -> int:
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    _, profile, claude = setup
    status = asyncio.run(fetch_status(claude, profile))
    if status is None:
        return fail(bool(args.json), "could not run `claude auth status`")
    if args.json:
        emit_json(status_doc(profile, status))
    else:
        print(render_status(profile, status))
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    cfg, profile, claude = setup
    # With --json our stdout is the result document: claude's login UI goes to stderr instead,
    # so that is the stream that must be a terminal for the in-place (tty) sign-in.
    ui_tty = (sys.stderr if as_json else sys.stdout).isatty()
    mode = choose_login_mode(
        stdin_tty=sys.stdin.isatty(), stdout_tty=ui_tty, terminal=args.terminal
    )
    label = f"{profile.emoji} {profile.name}".strip()
    if not as_json:
        if mode == "tty":
            print(f"Signing in {label} ({profile.config_dir}) via claude.ai…", flush=True)
        elif mode == "headless":
            eprint(f"Opening your browser to sign in {label} via claude.ai (waiting up to 10 min)…")
        else:
            eprint(f"Opening a Terminal window to sign in {label} (waiting up to 10 min)…")
    try:
        outcome = asyncio.run(login(cfg, profile, claude, mode, ui_to_stderr=as_json))
    except KeyboardInterrupt:
        return fail(as_json, "sign-in cancelled", code=EXIT_CANCELLED)
    refreshed = request_daemon_refresh(profile.id) if outcome.logged_in else False
    status = outcome.status
    if as_json:
        emit_json(
            {
                "ok": outcome.logged_in,
                "profile_id": profile.id,
                "mode": mode,
                "logged_in": outcome.logged_in,
                "account": status.account if status is not None else None,
                "subscription_type": status.subscription_type if status is not None else None,
                "daemon_refreshed": refreshed,
                "error": outcome.error,
            }
        )
    elif outcome.logged_in and status is not None:
        plan = f" · {status.subscription_type}" if status.subscription_type else ""
        print(f"Signed in {label} as {status.account or 'unknown account'}{plan}.")
    else:
        eprint(f"ccs: {outcome.error or 'not signed in'}")
    return EXIT_OK if outcome.logged_in else EXIT_ERROR


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_logout(args: argparse.Namespace) -> int:
    as_json = bool(args.json)
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    _, profile, claude = setup
    # `--json` means the caller (the app) already confirmed.
    if not as_json and not _confirm(
        f"Log out {profile.emoji} {profile.name} ({profile.config_dir})? [y/N] "
    ):
        print("Cancelled.")
        return EXIT_ERROR
    status, error = asyncio.run(logout(claude, profile))
    logged_in = status is not None and status.logged_in
    refreshed = request_daemon_refresh(profile.id)
    if as_json:
        emit_json(
            {
                "ok": error is None,
                "profile_id": profile.id,
                "logged_in": logged_in,
                "daemon_refreshed": refreshed,
                "error": error,
            }
        )
    elif error is None:
        print(f"Logged out {f'{profile.emoji} {profile.name}'.strip()}.")
    else:
        eprint(f"ccs: {error}")
    return EXIT_OK if error is None else EXIT_ERROR


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", required=True, metavar="<id>", help="profile id")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    auth = sub.add_parser("auth", help="sign in, check or sign out a profile's Claude Code login")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True, metavar="<command>")
    p = auth_sub.add_parser("login", help="sign in via claude.ai (opens the browser)")
    p.add_argument(
        "--terminal", action="store_true", help="sign in from a new Terminal window instead"
    )
    _common(p)
    p.set_defaults(func=cmd_login)
    p = auth_sub.add_parser("status", help="show whether the profile is signed in")
    _common(p)
    p.set_defaults(func=cmd_status)
    p = auth_sub.add_parser("logout", help="sign the profile out (asks first)")
    _common(p)
    p.set_defaults(func=cmd_logout)
