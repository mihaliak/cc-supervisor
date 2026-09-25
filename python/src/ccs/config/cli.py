"""`ccs config …` and `ccs profile …` subcommands (ADR-0017)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from ccs import paths
from ccs.config import store
from ccs.config.defaults import default_config_dict, default_profile_dict
from ccs.config.models import Config, Profile
from ccs.config.validate import validate
from ccs.output import EXIT_OK, EXIT_USAGE, UsageError, emit_json, eprint, fail

Handler = Callable[[argparse.Namespace], int]

# Top-level keys `ccs config set` may not touch.
_CONFIG_SET_FORBIDDEN = {
    "profiles": "use `ccs profile …` to change profiles",
    "version": "managed automatically",
    "revision": "managed automatically",
}


def parse_value(text: str) -> Any:
    """JSON when it parses (numbers, booleans, null, arrays, objects, quoted strings), else text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_assignments(items: list[str]) -> list[tuple[list[str], Any]]:
    """Parse `dotted.key=value` items into `(key parts, value)` pairs."""
    out: list[tuple[list[str], Any]] = []
    for item in items:
        key, sep, value = item.partition("=")
        parts = key.split(".")
        if not sep or not key or any(not part for part in parts):
            raise UsageError(f"expected <dotted.key>=<value>, got '{item}'")
        out.append((parts, parse_value(value)))
    return out


def set_dotted(target: dict[str, Any], parts: list[str], value: Any) -> None:
    """Set `target[a][b]…[z] = value`, creating intermediate objects."""
    node = target
    for i, part in enumerate(parts[:-1]):
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            if nxt is not None:
                raise UsageError(f"'{'.'.join(parts[: i + 1])}' is not an object")
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _issues(exc: store.ConfigInvalid) -> list[dict[str, str]]:
    return [i.to_dict() for i in exc.issues]


def _run(args: argparse.Namespace, body: Callable[[], int]) -> int:
    """Run a command body, mapping store/usage errors to exit codes and output."""
    as_json = bool(getattr(args, "json", False))
    try:
        return body()
    except UsageError as exc:
        return fail(as_json, str(exc), code=EXIT_USAGE)
    except store.ConfigInvalid as exc:
        return fail(as_json, str(exc), issues=_issues(exc))
    except store.ConfigError as exc:
        return fail(as_json, str(exc))


def _profile_summary(cfg: Config) -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "flag": p.flag,
            "name": p.name,
            "emoji": p.emoji,
            "config_dir": p.config_dir,
            "default": p.id == cfg.default_profile,
        }
        for p in cfg.profiles
    ]


# --- ccs config -------------------------------------------------------------------------


def cmd_config_path(args: argparse.Namespace) -> int:
    if args.json:
        emit_json({"ok": True, "path": str(paths.config_file())})
    else:
        print(paths.config_file())
    return EXIT_OK


def cmd_config_show(args: argparse.Namespace) -> int:
    def body() -> int:
        cfg = store.ensure_config()
        effective = cfg.to_dict()
        if args.json:
            emit_json({"ok": True, "path": str(paths.config_file()), "config": effective})
        else:
            print(json.dumps(effective, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK

    return _run(args, body)


def cmd_config_validate(args: argparse.Namespace) -> int:
    path = paths.config_file()
    revision: int | None = None
    try:
        raw = store.load_raw(path)
    except store.ConfigMissing:
        issues = [{"path": "", "message": f"config file not found: {path}"}]
    except store.ConfigInvalid as exc:
        issues = _issues(exc)
    else:
        rev = raw.get("revision")
        revision = rev if isinstance(rev, int) and not isinstance(rev, bool) else None
        issues = [i.to_dict() for i in validate(raw)]
    ok = not issues
    if args.json:
        emit_json({"ok": ok, "issues": issues, "revision": revision, "path": str(path)})
    elif ok:
        print(f"ok: {path} (revision {revision})")
    else:
        print(f"invalid: {path}")
        for issue in issues:
            print(f"  {issue['path'] or '<root>'}: {issue['message']}")
    return EXIT_OK if ok else 1


def cmd_config_defaults(args: argparse.Namespace) -> int:
    doc = {
        "ok": True,
        "config": default_config_dict(),
        "profile": default_profile_dict("", "", "", "", ""),
    }
    if args.json:
        emit_json(doc)
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_config_set(args: argparse.Namespace) -> int:
    def body() -> int:
        assignments = parse_assignments(args.assignments)
        for parts, _ in assignments:
            reason = _CONFIG_SET_FORBIDDEN.get(parts[0])
            if reason:
                raise UsageError(f"can't set '{parts[0]}': {reason}")
        store.ensure_config()

        def mutate(raw: dict[str, Any]) -> None:
            for parts, value in assignments:
                set_dotted(raw, parts, value)

        cfg = store.save(mutate)
        if args.json:
            emit_json({"ok": True, "revision": cfg.revision})
        else:
            print(f"saved (revision {cfg.revision})")
        return EXIT_OK

    return _run(args, body)


# --- ccs profile ------------------------------------------------------------------------


def cmd_profile_list(args: argparse.Namespace) -> int:
    def body() -> int:
        cfg = store.ensure_config()
        rows = _profile_summary(cfg)
        if args.json:
            emit_json({"ok": True, "default_profile": cfg.default_profile, "profiles": rows})
        else:
            for r in rows:
                mark = "*" if r["default"] else " "
                ident = f"{mark} {r['emoji']} {r['id']:<12} --{r['flag']:<12}"
                print(f"{ident} {r['name']}  {r['config_dir']}")
        return EXIT_OK

    return _run(args, body)


def cmd_profile_show(args: argparse.Namespace) -> int:
    def body() -> int:
        cfg = store.ensure_config()
        prof = cfg.profile(args.id)
        if prof is None:
            raise store.ConfigError(f"no profile with id '{args.id}'")
        doc = prof.to_dict()
        if args.json:
            emit_json({"ok": True, "profile": doc, "default": prof.id == cfg.default_profile})
        else:
            print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK

    return _run(args, body)


def cmd_profile_add(args: argparse.Namespace) -> int:
    def body() -> int:
        cfg = store.ensure_config()
        flag = args.flag or args.id
        name = args.name or args.id.replace("-", " ").title()
        new = default_profile_dict(args.id, flag, name, args.emoji, args.config_dir)
        make_default = bool(args.default) or not cfg.profiles

        def mutate(raw: dict[str, Any]) -> None:
            profiles = raw.setdefault("profiles", [])
            if not isinstance(profiles, list):
                raise UsageError("config 'profiles' is not a list")
            profiles.append(new)
            if make_default:
                raw["default_profile"] = args.id

        saved = store.save(mutate)
        if args.json:
            emit_json({"ok": True, "revision": saved.revision, "profile": new})
        else:
            print(f"added profile '{args.id}' (ccs --{flag})")
        return EXIT_OK

    return _run(args, body)


def revert_statusline(profile: Profile) -> dict[str, Any]:
    """Undo an applied statusline before its profile goes away (`result`, maybe `hint`).

    A conflict or error doesn't block the removal; the hint tells the user what to fix.
    """
    from ccs.statusline import apply as statusline_apply  # statusline imports ccs.config

    try:
        return statusline_apply.revert(profile).to_dict()
    except (statusline_apply.ApplyError, OSError, ValueError) as exc:
        path = statusline_apply.settings_path(profile)
        return {
            "profile_id": profile.id,
            "result": "error",
            "restored": None,
            "hint": f"could not revert the statusline ({exc}); fix statusLine in {path}",
        }


def cmd_profile_remove(args: argparse.Namespace) -> int:
    def body() -> int:
        cfg = store.ensure_config()
        profile = cfg.profile(args.id)
        if profile is None:
            raise store.ConfigError(f"no profile with id '{args.id}'")
        others = [p.id for p in cfg.profiles if p.id != args.id]
        new_default: str | None = args.default
        if new_default is not None and new_default not in others:
            raise UsageError(f"--default '{new_default}' is not another existing profile")
        if cfg.default_profile == args.id and others and new_default is None:
            raise UsageError(
                f"'{args.id}' is the default profile; pass --default <other> ({', '.join(others)})"
            )

        def mutate(raw: dict[str, Any]) -> None:
            raw["profiles"] = [
                p
                for p in raw.get("profiles", [])
                if not (isinstance(p, dict) and p.get("id") == args.id)
            ]
            if new_default is not None:
                raw["default_profile"] = new_default

        statusline = revert_statusline(profile)
        saved = store.save(mutate)
        hint = statusline.get("hint")
        if args.json:
            emit_json(
                {
                    "ok": True,
                    "revision": saved.revision,
                    "removed": args.id,
                    "statusline": statusline,
                }
            )
        else:
            print(f"removed profile '{args.id}' (config dir and login untouched)")
            if statusline.get("result") == "reverted":
                print(f"restored the previous statusLine of '{args.id}'")
            if hint:
                eprint(f"ccs: {hint}")
        return EXIT_OK

    return _run(args, body)


def cmd_profile_set(args: argparse.Namespace) -> int:
    def body() -> int:
        assignments = parse_assignments(args.assignments)
        for parts, _ in assignments:
            if parts[0] == "id":
                raise UsageError("a profile's id is immutable")
        cfg = store.ensure_config()
        if cfg.profile(args.id) is None:
            raise store.ConfigError(f"no profile with id '{args.id}'")

        def mutate(raw: dict[str, Any]) -> None:
            for prof in raw.get("profiles", []):
                if isinstance(prof, dict) and prof.get("id") == args.id:
                    for parts, value in assignments:
                        set_dotted(prof, parts, value)
                    return
            raise store.ConfigError(f"no profile with id '{args.id}'")

        saved = store.save(mutate)
        if args.json:
            emit_json({"ok": True, "revision": saved.revision})
        else:
            print(f"saved profile '{args.id}' (revision {saved.revision})")
        return EXIT_OK

    return _run(args, body)


# --- registration -----------------------------------------------------------------------


def _json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="machine-readable output")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `config` and `profile` subcommands."""
    cfg = sub.add_parser("config", help="show, validate and edit the config file")
    cfg_sub = cfg.add_subparsers(dest="config_cmd", required=True, metavar="<command>")

    p = cfg_sub.add_parser("path", help="print the config file path")
    _json_flag(p)
    p.set_defaults(func=cmd_config_path)
    p = cfg_sub.add_parser("show", help="print the effective config (defaults filled in)")
    _json_flag(p)
    p.set_defaults(func=cmd_config_show)
    p = cfg_sub.add_parser("validate", help="check the config file")
    _json_flag(p)
    p.set_defaults(func=cmd_config_validate)
    p = cfg_sub.add_parser("defaults", help="print every default value")
    _json_flag(p)
    p.set_defaults(func=cmd_config_defaults)
    p = cfg_sub.add_parser("set", help="change top-level keys: <dotted.key>=<value>…")
    p.add_argument("assignments", nargs="+", metavar="key=value")
    _json_flag(p)
    p.set_defaults(func=cmd_config_set)

    prof = sub.add_parser("profile", help="list, add, remove and edit profiles")
    prof_sub = prof.add_subparsers(dest="profile_cmd", required=True, metavar="<command>")

    p = prof_sub.add_parser("list", help="list profiles")
    _json_flag(p)
    p.set_defaults(func=cmd_profile_list)
    p = prof_sub.add_parser("show", help="show one profile (defaults filled in)")
    p.add_argument("id")
    _json_flag(p)
    p.set_defaults(func=cmd_profile_show)
    p = prof_sub.add_parser("add", help="add a profile")
    p.add_argument("--id", required=True)
    p.add_argument("--flag", help="launcher flag (default: the id)")
    p.add_argument("--name", help="display name (default: from the id)")
    p.add_argument("--emoji", required=True)
    p.add_argument("--config-dir", required=True, dest="config_dir")
    p.add_argument("--default", action="store_true", help="make it the default profile")
    _json_flag(p)
    p.set_defaults(func=cmd_profile_add)
    p = prof_sub.add_parser("remove", help="remove a profile (its config dir is untouched)")
    p.add_argument("id")
    p.add_argument("--default", metavar="OTHER", help="new default when removing the default")
    _json_flag(p)
    p.set_defaults(func=cmd_profile_remove)
    p = prof_sub.add_parser("set", help="edit a profile: <dotted.key>=<value>…")
    p.add_argument("id")
    p.add_argument("assignments", nargs="+", metavar="key=value")
    _json_flag(p)
    p.set_defaults(func=cmd_profile_set)
