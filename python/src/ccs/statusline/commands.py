"""`ccs statusline generate|apply|revert|preview --profile <id> [--json]` (ADR-0017)."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, tzinfo
from typing import Any

from ccs.clock import SystemClock, local_tz
from ccs.config import store
from ccs.config.models import Config, Profile
from ccs.output import EXIT_ERROR, EXIT_OK, EXIT_USAGE, emit_json, fail
from ccs.statusline import apply as apply_mod
from ccs.statusline import template
from ccs.statusline.render import Limits, RenderContext, render, to_ansi, to_plain
from ccs.usage.model import format_iso, to_utc_seconds

PREVIEW_STATES = (
    "normal",
    "warn",
    "paused",
    "overridden",
    "no_data",
    "weekly_warn",
    "model_scoped_warn",
    "spill",
    "supervisor_offline",
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("statusline", help="generate / apply / revert / preview the statusline")
    actions = p.add_subparsers(dest="statusline_action", metavar="<action>")
    specs = (
        ("generate", "write <config_dir>/ccs-statusline.py", cmd_generate),
        ("apply", "point settings.json statusLine at the script (with backup)", cmd_apply),
        ("revert", "restore the previous statusLine", cmd_revert),
        ("preview", "render sample states", cmd_preview),
    )
    for name, help_text, func in specs:
        a = actions.add_parser(name, help=help_text)
        a.add_argument("--profile", metavar="<id>", required=True, help="profile id")
        a.add_argument("--json", action="store_true", help="machine-readable output")
        a.set_defaults(func=func)
    p.set_defaults(func=lambda args: _help(p))


def _help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return EXIT_USAGE


def _load(args: argparse.Namespace) -> tuple[Config, Profile] | int:
    as_json = bool(args.json)
    try:
        cfg = store.ensure_config()
    except store.ConfigError as exc:
        return fail(as_json, str(exc))
    profile = cfg.profile(args.profile)
    if profile is None:
        return fail(as_json, f"unknown profile '{args.profile}'", code=EXIT_USAGE)
    return cfg, profile


def cmd_generate(args: argparse.Namespace) -> int:
    loaded = _load(args)
    if isinstance(loaded, int):
        return loaded
    cfg, profile = loaded
    try:
        result = template.generate(profile, cfg)
    except OSError as exc:
        return fail(bool(args.json), f"cannot write statusline script: {exc}")
    if args.json:
        emit_json({"ok": True, **result.to_dict()})
    else:
        state = "written" if result.changed else "up to date"
        print(f"{result.path} ({state})")
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    loaded = _load(args)
    if isinstance(loaded, int):
        return loaded
    cfg, profile = loaded
    try:
        result = apply_mod.apply(profile, cfg)
    except apply_mod.ApplyError as exc:
        return fail(bool(args.json), str(exc), issues=[{"path": exc.code, "message": str(exc)}])
    except OSError as exc:
        return fail(bool(args.json), f"apply failed: {exc}")
    if args.json:
        emit_json({"ok": True, **result.to_dict()})
    elif result.result == apply_mod.ALREADY_APPLIED:
        print(f"{result.settings_path}: already applied")
    else:
        backup = f" (backup: {result.backup_path})" if result.backup_path else ""
        print(f"{result.settings_path}: statusLine → {result.command}{backup}")
    return EXIT_OK


def cmd_revert(args: argparse.Namespace) -> int:
    loaded = _load(args)
    if isinstance(loaded, int):
        return loaded
    _, profile = loaded
    try:
        result = apply_mod.revert(profile)
    except apply_mod.ApplyError as exc:
        return fail(bool(args.json), str(exc), issues=[{"path": exc.code, "message": str(exc)}])
    except OSError as exc:
        return fail(bool(args.json), f"revert failed: {exc}")
    ok = result.result != apply_mod.CONFLICT
    if args.json:
        emit_json({"ok": ok, **result.to_dict()})
    elif result.result == apply_mod.NOT_APPLIED:
        print(f"{profile.id}: statusline was not applied; nothing to revert")
    elif result.result == apply_mod.REVERTED:
        print(f"{profile.id}: statusLine restored")
    else:
        print(f"ccs: {result.hint}")
    return EXIT_OK if ok else EXIT_ERROR


# ---------------------------------------------------------------- preview


def _stdin(now: datetime, session: float | None, weekly: float | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "session_id": "preview",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "effort": {"level": "xhigh"},
        "workspace": {"current_dir": "/Users/you/Code/project"},
    }
    limits: dict[str, Any] = {}
    if session is not None:
        reset = int((now + timedelta(hours=2, minutes=13)).timestamp())
        limits["five_hour"] = {"used_percentage": session, "resets_at": reset}
    if weekly is not None:
        reset = int((now + timedelta(days=2, hours=3)).timestamp())
        limits["seven_day"] = {"used_percentage": weekly, "resets_at": reset}
    if limits:
        data["rate_limits"] = limits
    return data


def _usage(now: datetime, **overrides: Any) -> dict[str, Any]:
    iso = format_iso(now)
    usage: dict[str, Any] = {
        "schema": 1,
        "profile_id": "preview",
        "status": "ok",
        "fetched_at": iso,
        "polled_at": iso,
        "windows": {"session": None, "weekly": None, "model_scoped": []},
        "extra_usage": None,
    }
    usage.update(overrides)
    return usage


def preview_contexts(
    profile: Profile, cfg: Config, now: datetime, tz: tzinfo
) -> list[tuple[str, RenderContext]]:
    """Fixed fake contexts per state, with the real profile name/emoji and thresholds."""
    limits = Limits.from_mapping(profile.limits.to_dict())
    colors = cfg.display.colors

    def ctx(
        stdin: dict[str, Any],
        *,
        usage: dict[str, Any] | None = None,
        record: dict[str, Any] | None = None,
        offline: bool = False,
        spill: bool | None = None,
    ) -> RenderContext:
        lim = limits
        if spill is not None:
            lim = Limits.from_mapping(
                {
                    **profile.limits.to_dict(),
                    "extra_usage": {**profile.limits.extra_usage.to_dict(), "spill": spill},
                }
            )
        return RenderContext(
            profile_id=profile.id,
            name=profile.name,
            emoji=profile.emoji,
            limits=lim,
            now=now,
            tz=tz,
            stdin=stdin,
            usage=usage,
            session_record=record,
            wrapper_id="preview" if record is not None else None,
            supervisor_offline=offline,
            yellow_from=colors.yellow_from,
            red_from=colors.red_from,
        )

    def record(state: str, resume: datetime | None, holds: list[str]) -> dict[str, Any]:
        return {
            "wrapper_id": "preview",
            "supervision": {
                "state": state,
                "holds": holds,
                "resume_at": format_iso(resume),
            },
        }

    fable_reset = format_iso(now + timedelta(days=2, hours=3))
    scoped = _usage(
        now,
        windows={
            "session": None,
            "weekly": None,
            "model_scoped": [
                {
                    "name": "Fable",
                    "percent": 82,
                    "resets_at": fable_reset,
                    "observed_at": format_iso(now),
                }
            ],
        },
    )
    extra = _usage(
        now,
        extra_usage={
            "enabled": True,
            "percent": 32,
            "used": 3.2,
            "limit": 10.0,
            "currency": "EUR",
            "disabled_reason": None,
        },
    )
    resume = now + timedelta(minutes=42)
    samples = {
        "normal": ctx(_stdin(now, 45, 50)),
        "warn": ctx(_stdin(now, 84, 50)),
        "paused": ctx(_stdin(now, 90, 50), record=record("paused", resume, ["session"])),
        "overridden": ctx(_stdin(now, 91, 50), record=record("overridden", resume, ["session"])),
        "no_data": ctx(_stdin(now, None, None)),
        "weekly_warn": ctx(_stdin(now, 45, 86)),
        "model_scoped_warn": ctx(_stdin(now, 45, 50), usage=scoped),
        "spill": ctx(_stdin(now, 100, 50), usage=extra, spill=True),
        "supervisor_offline": ctx(_stdin(now, 45, 50), offline=True),
    }
    return [(state, samples[state]) for state in PREVIEW_STATES]


def cmd_preview(args: argparse.Namespace) -> int:
    loaded = _load(args)
    if isinstance(loaded, int):
        return loaded
    cfg, profile = loaded
    now = to_utc_seconds(SystemClock().now())
    items = []
    for state, context in preview_contexts(profile, cfg, now, local_tz()):
        segments = render(context)
        items.append(
            {
                "state": state,
                "plain": to_plain(segments),
                "ansi": to_ansi(segments),
                "segments": [s.to_dict() for s in segments],
            }
        )
    script = template.script_path(profile)
    status = {
        "applied": apply_mod.is_applied(profile),
        "script_path": str(script),
        "script_current": template.script_is_current(profile, cfg),
    }
    if args.json:
        emit_json({"ok": True, "profile_id": profile.id, "status": status, "samples": items})
        return EXIT_OK
    width = max(len(s) for s in PREVIEW_STATES)
    for item in items:
        print(f"{item['state']:<{width}}  {item['ansi']}")
    return EXIT_OK
