"""`ccs` command-line entry point (ADR-0017)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import cast

from ccs import __version__
from ccs.config import cli as config_cli
from ccs.usage import cli as usage_cli


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Supervise Claude Code usage across multiple profiles.",
    )
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    config_cli.register(sub)
    usage_cli.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run `ccs`; returns the process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return cast(Callable[[argparse.Namespace], int], func)(args)
