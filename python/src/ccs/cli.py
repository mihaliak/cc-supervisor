"""`ccs` command-line entry point (ADR-0017).

`ccs [--<flag> | --profile <id>] [--force] [--no-supervise] [claude args…]` is the launcher
(ADR-0006); anything starting with a subcommand, `--help` or `--version` goes to argparse.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import cast

from ccs import __version__
from ccs import auth as auth_cli
from ccs.config import cli as config_cli
from ccs.config import store
from ccs.config.models import Config
from ccs.daemon import cli as daemon_cli
from ccs.launcher.args import LaunchArgsError, LaunchSpec, is_management
from ccs.launcher.args import parse_launcher_args as _parse_launcher_args
from ccs.output import EXIT_ERROR, EXIT_USAGE, eprint
from ccs.statusline import commands as statusline_cli
from ccs.usage import cli as usage_cli

__all__ = ["build_parser", "launch", "main", "parse_launcher_args"]


def parse_launcher_args(argv: Sequence[str], config: Config) -> LaunchSpec | None:
    """Launcher pre-parse (see `ccs.launcher.args`)."""
    return _parse_launcher_args(argv, config)


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Supervise Claude Code usage across multiple profiles.",
        epilog="Launcher: ccs [--<profile-flag> | --profile <id>] [--force] "
        "[--no-supervise] [claude args…]",
    )
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    config_cli.register(sub)
    usage_cli.register(sub)
    daemon_cli.register(sub)
    auth_cli.register(sub)
    statusline_cli.register(sub)
    return parser


def launch(argv: Sequence[str]) -> int:
    """`ccs --<flag> …`: load (or seed) the config, pre-parse, run claude."""
    try:
        config = store.ensure_config()
    except store.ConfigError as exc:
        eprint(f"ccs: {exc}")
        return EXIT_ERROR
    try:
        spec = parse_launcher_args(argv, config)
    except LaunchArgsError as exc:
        eprint(f"ccs: {exc}")
        return EXIT_USAGE
    if spec is None:  # pragma: no cover - guarded by is_management in main()
        return EXIT_USAGE
    from ccs.launcher import main as launcher_main

    return launcher_main.run(spec, config)


def main(argv: list[str] | None = None) -> int:
    """Run `ccs`; returns the process exit code."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not is_management(args_list):
        return launch(args_list)
    parser = build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return cast(Callable[[argparse.Namespace], int], func)(args)
