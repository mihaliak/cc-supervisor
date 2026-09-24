"""Real `claude` probe (read-only). Runs only with `make test-live` (CCS_TEST_LIVE=1)."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from usage_helpers import make_profile

from ccs.claude_cli import resolve_claude
from ccs.clock import SystemClock
from ccs.usage.model import STATUS_NO_SUBSCRIPTION, STATUS_OK
from ccs.usage.source_claude import fetch_snapshot


@pytest.mark.live
def test_live_probe_personal_profile() -> None:
    profile = make_profile(os.path.expanduser("~/.claude"), pid="personal")
    started = time.monotonic()
    snap = asyncio.run(
        fetch_snapshot(None, profile, clock=SystemClock(), claude=resolve_claude(None))
    )
    assert time.monotonic() - started < 5.0
    assert snap.status in (STATUS_OK, STATUS_NO_SUBSCRIPTION), snap.error
