"""Launcher pre-parse (ADR-0006, ADR-0017).

`ccs [--<flag> | --profile <id>] [--force] [--no-supervise] [claude args…]`: ccs-only options
are consumed from the front of argv, in any order, until the first token that is not one;
everything from there on goes to `claude` untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ccs.config.models import Config, Profile
from ccs.config.validate import CCS_RESERVED, CLAUDE_LONG_OPTIONS

# argv[0] values handled by argparse (management subcommands, ADR-0017).
SUBCOMMANDS = frozenset(
    {
        "status",
        "usage",
        "sessions",
        "events",
        "pause",
        "resume",
        "warmup",
        "auth",
        "statusline",
        "profile",
        "config",
        "daemon",
        "doctor",
    }
)
ARGPARSE_TOKENS = frozenset({"-h", "--help", "--version"})


class LaunchArgsError(Exception):
    """A launcher usage error (exit code 2)."""


@dataclass(frozen=True)
class LaunchSpec:
    """What `ccs --<flag> …` should run."""

    profile: Profile
    force: bool
    no_supervise: bool
    claude_args: tuple[str, ...]


def is_management(argv: Sequence[str]) -> bool:
    """True when argparse (a subcommand, `--help`, `--version`) handles `argv`."""
    return bool(argv) and (argv[0] in SUBCOMMANDS or argv[0] in ARGPARSE_TOKENS)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def typo_suggestion(name: str, config: Config) -> str | None:
    """A profile flag `name` is likely a typo of (distance ≤ 2 and shorter than the flag)."""
    if name in CLAUDE_LONG_OPTIONS or name in CCS_RESERVED:
        return None
    best: tuple[int, str] | None = None
    for profile in config.profiles:
        dist = edit_distance(name, profile.flag)
        if dist <= 2 and dist < len(profile.flag) and (best is None or dist < best[0]):
            best = (dist, profile.flag)
    return best[1] if best else None


def _select(current: Profile | None, new: Profile) -> Profile:
    if current is not None and current.id != new.id:
        raise LaunchArgsError(f"two profiles given (--{current.flag} and --{new.flag}); pick one")
    return new


def parse_launcher_args(argv: Sequence[str], config: Config) -> LaunchSpec | None:
    """Split `argv` into the launcher spec, or `None` when argparse handles it."""
    if is_management(argv):
        return None
    profile: Profile | None = None
    force = False
    no_supervise = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--force":
            force = True
        elif tok == "--no-supervise":
            no_supervise = True
        elif tok == "--profile" or tok.startswith("--profile="):
            if tok == "--profile":
                if i + 1 >= len(argv):
                    raise LaunchArgsError("--profile needs a profile id")
                pid = argv[i + 1]
                i += 1
            else:
                pid = tok.split("=", 1)[1]
            found = config.profile(pid)
            if found is None:
                known = ", ".join(p.id for p in config.profiles) or "none"
                raise LaunchArgsError(f"unknown profile '{pid}' (known: {known})")
            profile = _select(profile, found)
        elif tok.startswith("--") and len(tok) > 2 and "=" not in tok:
            name = tok[2:]
            by_flag = config.profile_by_flag(name)
            if by_flag is None:
                guess = typo_suggestion(name, config)
                if guess is not None:
                    raise LaunchArgsError(f"unknown profile '{tok}'. Did you mean --{guess}?")
                break
            profile = _select(profile, by_flag)
        else:
            break
        i += 1
    if profile is None:
        profile = config.default()
        if profile is None and len(config.profiles) == 1:
            profile = config.profiles[0]
        if profile is None:
            if not config.profiles:
                raise LaunchArgsError("no profiles configured (ccs profile add …)")
            flags = " ".join(f"--{p.flag}" for p in config.profiles)
            raise LaunchArgsError(f"no default profile; pass one of: {flags}")
    return LaunchSpec(
        profile=profile,
        force=force,
        no_supervise=no_supervise,
        claude_args=tuple(argv[i:]),
    )
