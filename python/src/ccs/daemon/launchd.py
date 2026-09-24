"""LaunchAgent management for `ccs daemon` (ADR-0006, ADR-0016).

All `launchctl` calls go through an injectable runner so tests never touch the real
launchd. Domain: `gui/<uid>`; label `local.ccsupervisor.daemon`.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccs import paths

LABEL = "local.ccsupervisor.daemon"
PASSTHROUGH_ENV = ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")


@dataclass(frozen=True)
class RunResult:
    rc: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], RunResult]


def default_runner(argv: list[str]) -> RunResult:
    """Run `argv` (a `launchctl` call) with a timeout."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RunResult(1, "", str(exc))
    return RunResult(done.returncode, done.stdout, done.stderr)


def plist_dir() -> Path:
    """`~/Library/LaunchAgents`."""
    return Path(os.path.expanduser("~/Library/LaunchAgents"))


def plist_path() -> Path:
    return plist_dir() / f"{LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain()}/{LABEL}"


def ccs_executable() -> str:
    """Absolute path of the `ccs` entry point: `shutil.which("ccs")`, else `sys.argv[0]`."""
    found = shutil.which("ccs")
    if found:
        return os.path.abspath(found)
    return os.path.abspath(sys.argv[0])


def install_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env captured at install (`PATH`, `HOME`, and XDG homes if set)."""
    src = os.environ if base is None else base
    return {k: src[k] for k in PASSTHROUGH_ENV if src.get(k)}


def render_plist(ccs_exec: str, env: Mapping[str, str], state_dir: Path) -> bytes:
    """The LaunchAgent plist (XML) for the daemon."""
    logs = Path(state_dir) / "logs"
    doc: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [ccs_exec, "daemon", "run"],
        "EnvironmentVariables": dict(sorted(env.items())),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
    }
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=True)


def parse_print(output: str) -> dict[str, Any]:
    """`launchctl print` → `{state, pid}` (best effort)."""
    state = re.search(r"^\s*state = (\S+)", output, re.MULTILINE)
    pid = re.search(r"^\s*pid = (\d+)", output, re.MULTILINE)
    return {
        "state": state.group(1) if state else None,
        "pid": int(pid.group(1)) if pid else None,
    }


class Launchd:
    """install / uninstall / start / stop / restart / status of the LaunchAgent."""

    def __init__(self, runner: Runner | None = None, plist: Path | None = None) -> None:
        self.runner = runner or default_runner
        self.plist = Path(plist) if plist else plist_path()

    def _launchctl(self, *args: str) -> RunResult:
        return self.runner(["launchctl", *args])

    def is_installed(self) -> bool:
        return self.plist.exists()

    def print_service(self) -> RunResult:
        return self._launchctl("print", service_target())

    def is_loaded(self) -> bool:
        return self.print_service().rc == 0

    def install(self, ccs_exec: str | None = None, env: Mapping[str, str] | None = None) -> None:
        """Write the plist, unload any old job, then bootstrap it (starts it: RunAtLoad)."""
        paths.ensure_state_layout()
        data = render_plist(ccs_exec or ccs_executable(), install_env(env), paths.state_dir())
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.plist.with_suffix(".plist.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.plist)
        self._launchctl("bootout", service_target())  # ignore errors: may not be loaded
        result = self._launchctl("bootstrap", domain(), str(self.plist))
        if result.rc != 0:
            raise LaunchdError(f"launchctl bootstrap failed: {result.stderr.strip() or result.rc}")

    def uninstall(self) -> None:
        self._launchctl("bootout", service_target())
        if self.plist.exists():
            self.plist.unlink()

    def start(self) -> None:
        """`bootstrap` when unloaded, else `kickstart`. Raises when not installed."""
        if not self.is_installed():
            raise LaunchdError("daemon is not installed (run: ccs daemon install)")
        if self.is_loaded():
            result = self._launchctl("kickstart", service_target())
        else:
            result = self._launchctl("bootstrap", domain(), str(self.plist))
        if result.rc != 0:
            raise LaunchdError(f"launchctl failed: {result.stderr.strip() or result.rc}")

    def stop(self) -> None:
        """`bootout` (unload), keeping the plist so `start` works again."""
        result = self._launchctl("bootout", service_target())
        if result.rc != 0 and self.is_loaded():
            raise LaunchdError(f"launchctl bootout failed: {result.stderr.strip() or result.rc}")

    def restart(self) -> None:
        if not self.is_loaded():
            self.start()
            return
        result = self._launchctl("kickstart", "-k", service_target())
        if result.rc != 0:
            raise LaunchdError(f"launchctl kickstart failed: {result.stderr.strip() or result.rc}")

    def status(self, *, probe: Callable[[], dict[str, Any] | None] | None = None) -> dict[str, Any]:
        """`{installed, loaded, state, pid, responsive, version, uptime_s, latency_ms}`."""
        printed = self.print_service()
        info = parse_print(printed.stdout) if printed.rc == 0 else {"state": None, "pid": None}
        out: dict[str, Any] = {
            "installed": self.is_installed(),
            "loaded": printed.rc == 0,
            "state": info["state"],
            "pid": info["pid"],
            "responsive": False,
            "version": None,
            "uptime_s": None,
            "latency_ms": None,
        }
        hello = (probe or socket_probe)()
        if hello is not None:
            out.update(hello)
        return out


class LaunchdError(Exception):
    """A `launchctl` operation failed."""


def socket_probe(timeout: float = 1.0) -> dict[str, Any] | None:
    """Round-trip `hello` + `status` on the socket → responsiveness, version, uptime."""
    from ccs.daemon.client import DaemonClient, DaemonUnavailable

    started = time.monotonic()
    try:
        with DaemonClient(timeout=timeout) as client:
            hello = client.hello()
            latency = (time.monotonic() - started) * 1000
            status = client.request("status")
    except DaemonUnavailable:
        return None
    if not hello.get("ok"):
        return None
    daemon = _dict(status.get("daemon"))
    info = _dict(hello.get("daemon"))
    return {
        "responsive": True,
        "version": info.get("version"),
        "daemon_pid": info.get("pid"),
        "uptime_s": daemon.get("uptime_s"),
        "latency_ms": round(latency, 1),
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# Module-level conveniences for the launcher's autostart (P05).


def is_installed() -> bool:
    return Launchd().is_installed()


def start() -> None:
    Launchd().start()
