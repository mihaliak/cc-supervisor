"""Daemon extension: keep existing statusline scripts current (P07, P04 hook API).

On every valid config load (startup included) each enabled profile whose script already exists
is regenerated when its content would differ (profile name/emoji/limits/colors, config dir, or
a `ccs` upgrade that changed the embedded sources). It never creates new scripts and never
touches `settings.json`: the launcher (`ensure_script`) and `ccs statusline apply` create them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ccs.config.models import Config
from ccs.statusline import template

if TYPE_CHECKING:
    from ccs.daemon.server import Daemon

log = logging.getLogger(__name__)


def refresh_scripts(config: Config) -> list[str]:
    """Regenerate existing scripts of enabled profiles; returns ids whose file changed."""
    changed: list[str] = []
    for profile in config.profiles:
        if not profile.statusline.enabled or not template.script_path(profile).exists():
            continue
        try:
            if template.generate(profile, config).changed:
                changed.append(profile.id)
        except Exception:
            log.exception("statusline regeneration failed for profile %s", profile.id)
    return changed


async def on_config_changed(old: Config | None, new: Config) -> None:
    changed = await asyncio.to_thread(refresh_scripts, new)
    if changed:
        log.info("statusline scripts regenerated: %s", ", ".join(changed))


def install(daemon: Daemon) -> None:
    """Register the config-change hook."""
    daemon.hooks.on_config_changed(on_config_changed)
