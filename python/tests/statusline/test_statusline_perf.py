"""Perf budget of the generated script: p95 < 60 ms over 50 runs (ADR-0013).

A heavily loaded machine (parallel builds, test runs) can push one measurement over budget, so a
miss is re-measured once after a short pause; only two consecutive misses fail the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sl_helpers import load_fixture, make_config, new_config_dir

from ccs.statusline import template

RUNS = 50
ATTEMPTS = 2
RETRY_PAUSE_S = 1.0
BUDGET_MS = float(os.environ.get("CCS_STATUSLINE_BUDGET_MS", "60"))


def measure(argv: list[str], stdin: bytes, env: dict[str, str]) -> list[float]:
    """Sorted wall times (ms) of `RUNS` script invocations, after one warm-up run."""
    subprocess.run(argv, input=stdin, capture_output=True, env=env, check=True)  # warm the cache
    times: list[float] = []
    for _ in range(RUNS):
        start = time.perf_counter()
        proc = subprocess.run(argv, input=stdin, capture_output=True, env=env)
        times.append((time.perf_counter() - start) * 1000)
        assert proc.returncode == 0
    return sorted(times)


@pytest.mark.perf
def test_generated_script_p95_under_budget(tmp_path: Path) -> None:
    config_dir = new_config_dir(tmp_path)
    cfg = make_config(config_dir)
    script = template.ensure_script(cfg.profiles[0], cfg)
    stdin = json.dumps(load_fixture("with_rate_limits.json")).encode()
    env = {"CCS_STATE_DIR": str(tmp_path / "state"), "CCS_WRAPPER_ID": "perf"}
    argv = [sys.executable, "-S", "-E", str(script)]
    misses: list[str] = []
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSE_S)
        times = measure(argv, stdin, env)
        p95 = times[int(RUNS * 0.95) - 1]
        if p95 < BUDGET_MS:
            return
        misses.append(f"p95 {p95:.1f} ms (p50 {times[RUNS // 2]:.1f} ms)")
    pytest.fail(f"over the {BUDGET_MS} ms budget {ATTEMPTS}x in a row: " + "; ".join(misses))
