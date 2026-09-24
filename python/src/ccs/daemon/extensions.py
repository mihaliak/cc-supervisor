"""Daemon extensions: modules exposing `install(daemon)` (P04 hook registry).

Later plans append their module path here (P06 supervisor engine, P07 statusline
regeneration, P08 warm-up scheduler, …). A module that fails to import or install is
logged and skipped, so one broken extension never takes the daemon down.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)

EXTENSIONS: tuple[str, ...] = ()

Installer = Callable[["Daemon"], Any]


def resolve(names: Iterable[str]) -> list[Installer]:
    """Import each module and return its `install` function (skipping failures)."""
    out: list[Installer] = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception:
            log.exception("daemon extension %s failed to import", name)
            continue
        install = getattr(module, "install", None)
        if callable(install):
            out.append(install)
        else:
            log.error("daemon extension %s has no install(daemon)", name)
    return out
