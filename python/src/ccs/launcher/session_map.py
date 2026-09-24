"""Map the PTY child's pid to its Claude Code session via `claude agents --json`.

P00-S3: interactive entries carry `pid`, `sessionId`, `status` (`busy|shell|idle|waiting`),
`kind: "interactive"`, `name`, `startedAt`, `cwd`. A submitted turn showed `busy` within
0.5 s and `idle` after an ESC.
"""

from __future__ import annotations

from dataclasses import dataclass

from ccs import claude_cli
from ccs.config.models import Profile

STATUSES = ("busy", "shell", "idle", "waiting")


@dataclass(frozen=True)
class SessionInfo:
    """The child's session id and activity (`unknown` when it could not be determined)."""

    session_id: str | None
    status: str


def is_busy(status: str) -> bool:
    """ADR-0007: anything other than `idle` counts as busy for pausing (incl. `unknown`)."""
    return status != "idle"


def is_known_busy(status: str) -> bool:
    """Busy per `claude agents` itself (`unknown` excluded)."""
    return status in ("busy", "shell", "waiting")


def find(entries: list[dict[str, object]], claude_pid: int) -> SessionInfo:
    """Pure lookup of `claude_pid` in an `agents --json` list."""
    for entry in entries:
        if entry.get("pid") == claude_pid:
            status = entry.get("status")
            sid = entry.get("sessionId")
            return SessionInfo(
                session_id=sid if isinstance(sid, str) and sid else None,
                status=status if isinstance(status, str) and status in STATUSES else "unknown",
            )
    return SessionInfo(None, "unknown")


async def lookup(
    claude: str, profile: Profile, claude_pid: int, *, timeout: float = 10.0
) -> SessionInfo:
    """The child's `SessionInfo`; `unknown` when `claude agents --json` fails."""
    try:
        entries = await claude_cli.agents_json(claude, profile, timeout=timeout)
    except claude_cli.ClaudeCliError:
        return SessionInfo(None, "unknown")
    return find(list(entries), claude_pid)
