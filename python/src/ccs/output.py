"""CLI output conventions (ADR-0013/0017): clean JSON on stdout, diagnostics on stderr."""

from __future__ import annotations

import json
import sys
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130  # Ctrl-C (128 + SIGINT)


class UsageError(Exception):
    """A user error in the command line (exit code 2)."""


def emit_json(obj: Any) -> None:
    """Print one JSON document on stdout."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def eprint(message: str) -> None:
    """Print a diagnostic line on stderr."""
    sys.stderr.write(message.rstrip("\n") + "\n")


def fail(
    as_json: bool,
    message: str,
    *,
    code: int = EXIT_ERROR,
    issues: list[dict[str, str]] | None = None,
) -> int:
    """Report an error in the requested format and return the exit code."""
    if as_json:
        emit_json({"ok": False, "error": message, "issues": issues or []})
    else:
        eprint(f"ccs: {message}")
        for issue in issues or []:
            eprint(f"  {issue.get('path') or '<root>'}: {issue.get('message')}")
    return code
