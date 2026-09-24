"""Helpers for supervisor engine tests: usage payloads and fake launcher clients."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ccs.daemon.client import AsyncDaemonClient
from ccs.usage.model import format_iso


def usage_payload(
    session: tuple[int, datetime] | None,
    weekly: tuple[int, datetime] | None = None,
    scoped: tuple[tuple[str, int, datetime], ...] = (),
) -> dict[str, Any]:
    """A minimal successful `get_usage` `response.response` object."""
    rl: dict[str, Any] = {
        "five_hour": (
            {"utilization": session[0], "resets_at": format_iso(session[1])} if session else None
        ),
        "seven_day": (
            {"utilization": weekly[0], "resets_at": format_iso(weekly[1])} if weekly else None
        ),
        "model_scoped": [
            {"display_name": n, "utilization": p, "resets_at": format_iso(r)} for n, p, r in scoped
        ],
        "extra_usage": None,
    }
    return {"subscription_type": "max", "rate_limits_available": True, "rate_limits": rl}


def set_usage(scenario_file: Path, payload: dict[str, Any]) -> None:
    """Rewrite the fake-claude scenario so the next probe returns `payload`."""
    spec = {"stream": {"get_usage": {"mode": "ok", "payload": payload}}}
    scenario_file.write_text(json.dumps(spec), encoding="utf-8")


class FakeLauncher:
    """Speaks the launcher side of the IPC protocol and auto-acks commands (P05 shapes)."""

    def __init__(self, wrapper_id: str, *, busy: bool = True, profile_id: str = "work") -> None:
        self.wrapper_id = wrapper_id
        self.profile_id = profile_id
        self.busy = busy
        self.cmds: list[dict[str, Any]] = []
        self.reply: dict[str, Any] = {}
        self.client = AsyncDaemonClient()
        self._task: asyncio.Task[None] | None = None

    async def start(self, **extra: Any) -> dict[str, Any]:
        await self.client.connect()
        await self.client.hello("launcher")
        self.reply = await self.client.request(
            "register_wrapper",
            wrapper_id=self.wrapper_id,
            profile_id=self.profile_id,
            wrapper_pid=os.getpid(),
            claude_pid=os.getpid(),
            cwd="/tmp/project",
            **extra,
        )
        self._task = asyncio.ensure_future(self._loop())
        return self.reply

    async def _loop(self) -> None:
        while True:
            msg = await self.client.next_push()
            if msg is None:
                return
            cmd = msg.get("cmd")
            if not isinstance(cmd, dict):
                continue
            self.cmds.append(cmd)
            if cmd.get("type") == "pause":
                result = "injected" if self.busy else "skipped"
                detail: dict[str, Any] = {
                    "was_busy": self.busy,
                    "session_id": f"s-{self.wrapper_id}",
                }
                self.busy = False
            else:
                result = "injected" if cmd.get("prompt") else "skipped"
                detail = {} if cmd.get("prompt") else {"reason": "no_prompt"}
            await self.client.send({"ack": cmd.get("cmd_id"), "result": result, "detail": detail})

    def types(self) -> list[str]:
        return [str(c.get("type")) for c in self.cmds]

    async def event(self, kind: str) -> dict[str, Any]:
        return await self.client.request(
            "wrapper_event", wrapper_id=self.wrapper_id, kind=kind, detail={}
        )

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self.client.close()
