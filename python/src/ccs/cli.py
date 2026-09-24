"""`ccs` command-line entry point (ADR-0017)."""

from __future__ import annotations

import argparse

from ccs import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Supervise Claude Code usage across multiple profiles.",
    )
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    parser.print_help()
    return 0
