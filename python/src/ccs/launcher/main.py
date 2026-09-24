"""`ccs --<flag> [claude args…]`: run the classic Claude Code TUI under supervision (ADR-0006).

Modes:
- **passthrough** (`-p`/`--print`, or stdin/stdout not a TTY): exec `claude` directly with the
  profile env. No PTY, no supervision, so pipes and scripts behave exactly like `claude`.
- **supervised interactive**: statusline injection, daemon link, start-while-held check,
  PTY proxy, command handling, unregister, claude's exit code.

The child env is the caller's env plus `CLAUDE_CONFIG_DIR` and the `CCS_*` variables, so
`ccs --work` behaves like `CLAUDE_CONFIG_DIR=~/.claude-work claude` (transparency). For the
default `~/.claude`, `CLAUDE_CONFIG_DIR` is removed instead (see `paths.apply_claude_config_dir`).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shlex
import signal
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ccs import claude_cli, paths
from ccs.clock import SystemClock, local_tz
from ccs.config.models import Config, Profile
from ccs.launcher import prompt, session_map
from ccs.launcher.args import LaunchSpec
from ccs.launcher.commands import CommandHandler, Timings
from ccs.launcher.daemon_link import DaemonLink, LaunchdApi, Registration
from ccs.launcher.pty_proxy import PtyProxy
from ccs.output import eprint

STATUSLINE_SCRIPT = "ccs-statusline.py"
ExecFn = Callable[[str, list[str], dict[str, str]], Any]


def is_print_mode(claude_args: Sequence[str]) -> bool:
    """`-p` / `--print` anywhere in the claude args."""
    return any(a in ("-p", "--print") or a.startswith("--print=") for a in claude_args)


def has_settings(claude_args: Sequence[str]) -> bool:
    return any(a == "--settings" or a.startswith("--settings=") for a in claude_args)


def launcher_env(
    profile: Profile, *, wrapper_id: str | None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The caller's env + the profile's config dir and the `CCS_*` launcher variables.

    `CLAUDE_CONFIG_DIR` is unset for the default `~/.claude`, so `ccs --personal` keeps using
    `~/.claude.json` exactly like plain `claude`.
    """
    env = dict(os.environ if base is None else base)
    env.pop("CCS_WRAPPER_ID", None)
    paths.apply_claude_config_dir(env, profile.config_dir)
    env["CCS_PROFILE"] = profile.id
    env["CCS_STATE_DIR"] = str(paths.state_dir())
    if wrapper_id:
        env["CCS_WRAPPER_ID"] = wrapper_id
    return env


def ensure_statusline(profile: Profile, config: Config | None = None) -> str | None:
    """The statusLine command for the profile, or None.

    Delegates to `ccs.statusline.apply.ensure_script(profile, config)` (P07: regenerates the
    script if missing/outdated). Fallback without P07: use the script only if it exists.
    """
    try:
        module = importlib.import_module("ccs.statusline.apply")
    except ImportError:
        module = None
    ensure = getattr(module, "ensure_script", None) if module is not None else None
    if callable(ensure):
        try:
            result = ensure(profile, config)
        except Exception as exc:
            eprint(f"ccs: statusline unavailable: {exc}")
            return None
        return result if isinstance(result, str) and result else None
    script = Path(profile.config_dir_env) / STATUSLINE_SCRIPT
    if not script.is_file():
        return None
    return f"{shlex.quote(sys.executable)} -S -E {shlex.quote(str(script))}"


def statusline_args(
    profile: Profile, claude_args: Sequence[str], config: Config | None = None
) -> list[str]:
    """`--settings '{"statusLine":…}'` to prepend, or [] (ADR-0006, ADR-0013)."""
    if not profile.statusline.enabled:
        return []
    if has_settings(claude_args):
        eprint(
            f"ccs: --settings given; statusline not injected "
            f"(run: ccs statusline apply --profile {profile.id})"
        )
        return []
    command = ensure_statusline(profile, config)
    if command is None:
        return []
    settings = {"statusLine": {"type": "command", "command": command}}
    return ["--settings", json.dumps(settings, separators=(",", ":"))]


def _reset_inherited_ignores() -> None:
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(sig, signal.SIG_DFL)


def exec_passthrough(claude: str, argv: list[str], env: dict[str, str]) -> None:
    """Replace this process with claude (print mode / non-TTY)."""
    _reset_inherited_ignores()
    os.execvpe(claude, argv, env)


class LauncherSession:
    """One supervised interactive run."""

    def __init__(
        self,
        *,
        profile: Profile,
        claude: str,
        argv: list[str],
        env: dict[str, str],
        wrapper_id: str,
        force: bool,
        link: DaemonLink | None,
        stdin_fd: int = 0,
        stdout_fd: int = 1,
        timings: Timings | None = None,
        lookup_timeout: float = 10.0,
    ) -> None:
        self.profile = profile
        self.claude = claude
        self.argv = argv
        self.env = env
        self.wrapper_id = wrapper_id
        self.force = force
        self.link = link
        self.stdin_fd = stdin_fd
        self.stdout_fd = stdout_fd
        self.timings = timings
        self.lookup_timeout = lookup_timeout
        self.proxy: PtyProxy | None = None
        self.handler: CommandHandler | None = None

    async def _held_check(self) -> tuple[bool, bool]:
        """`(start, started_overridden)` after the start-while-held question."""
        if self.link is None or self.link.state != "connected":
            return True, False
        info = prompt.held_info(await self.link.profile_status(), self.profile.id)
        if info is None:
            return True, False
        if self.force:
            return True, True
        now = SystemClock().now()
        question = prompt.held_prompt(self.profile.name, info, now, local_tz())
        if not prompt.ask_yes_no(question, self.stdin_fd, self.stdout_fd):
            return False, False
        return True, True

    async def run(self) -> int:
        link = self.link
        if link is not None:
            await link.connect()
        go, started_overridden = await self._held_check()
        if not go:
            if link is not None:
                await link.close(None)
            return 0
        proxy = PtyProxy(self.argv, self.env, stdin_fd=self.stdin_fd, stdout_fd=self.stdout_fd)
        self.proxy = proxy
        proxy.spawn()

        async def lookup() -> session_map.SessionInfo:
            return await session_map.lookup(
                self.claude, self.profile, proxy.claude_pid, timeout=self.lookup_timeout
            )

        report = link.wrapper_event if link is not None else None
        handler = CommandHandler(proxy, lookup, report, timings=self.timings)
        self.handler = handler
        proxy.on_user_submit = handler.on_user_submit
        if link is not None:
            link.start(
                Registration(
                    wrapper_id=self.wrapper_id,
                    profile_id=self.profile.id,
                    wrapper_pid=os.getpid(),
                    claude_pid=proxy.claude_pid,
                    cwd=os.getcwd(),
                    started_overridden=started_overridden,
                ),
                handler.handle,
                handler.apply_supervision,
            )
        code = 1
        try:
            code = await proxy.run()
        finally:
            if link is not None:
                await link.close(code)
        return code


def run(
    spec: LaunchSpec,
    config: Config,
    *,
    stdin_fd: int = 0,
    stdout_fd: int = 1,
    exec_fn: ExecFn | None = None,
    launchd: LaunchdApi | None = None,
    sock_path: Path | None = None,
    timings: Timings | None = None,
) -> int:
    """Run the launcher; returns the exit code (claude's in interactive mode)."""
    profile = spec.profile
    try:
        claude = claude_cli.resolve_claude(config)
    except claude_cli.ClaudeNotFound as exc:
        eprint(f"ccs: {exc}")
        return 127
    args = list(spec.claude_args)
    interactive = not is_print_mode(args) and os.isatty(stdin_fd) and os.isatty(stdout_fd)
    if not interactive:
        env = launcher_env(profile, wrapper_id=None)
        try:
            (exec_fn or exec_passthrough)(claude, [claude, *args], env)
        except OSError as exc:
            eprint(f"ccs: cannot run {claude}: {exc}")
            return 127
        return 0
    wrapper_id = str(uuid.uuid4())
    env = launcher_env(profile, wrapper_id=wrapper_id)
    argv = [claude, *statusline_args(profile, args, config), *args]
    link = None
    if not spec.no_supervise:
        link = DaemonLink(profile.id, wrapper_id, sock_path=sock_path, launchd=launchd, err=eprint)
    session = LauncherSession(
        profile=profile,
        claude=claude,
        argv=argv,
        env=env,
        wrapper_id=wrapper_id,
        force=spec.force,
        link=link,
        stdin_fd=stdin_fd,
        stdout_fd=stdout_fd,
        timings=timings,
    )
    return asyncio.run(session.run())
