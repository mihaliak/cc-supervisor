# ADR-0013: Python engineering standards

- Status: accepted
- Date: 2026-09-24
- Source: planner default

## Decision
- **Python ≥ 3.12**. Development machine: 3.14 via pyenv.
- **Runtime is stdlib only.** There are no third-party runtime dependencies, which keeps startup fast, installs trivial, and the statusline safe.
- **Packaging:**
  - `python/pyproject.toml` (hatchling, src layout); dist name `cc-supervisor`; import package `ccs`; entry point `ccs = ccs.cli:main`.
  - Install with `pipx install --editable ./python`, which gives `~/.local/bin/ccs`.
- **Dev tooling:** pytest, ruff (lint + format), mypy `--strict` on `ccs`. Make targets: `make test`, `make lint`, `make fmt`.
- **Architecture:**
  - **Pure core** (no IO): `config` models and validation, `usage.normalize`, `supervisor.policy`, `statusline.render`, `timefmt`, `warmup.rules`. Covered by table-driven unit tests with fixtures.
  - **IO shells:** subprocess (`claude`), PTY, sockets, files, launchd. These are thin and tested against a **fake `claude`** executable (`python/tests/fake_claude/`) that emulates:
    - `get_usage` responses, driven by a scenario file
    - `agents --json`
    - `auth status|login`
    - `-p` warm-ups
    - a minimal interactive TUI that reports received bytes, used for PTY tests
  - `claude_path` is injectable, so tests never touch the real `claude` unless you run `make test-live`.
- **Time:** inject a `Clock`. The core never calls `datetime.now()` or `time.time()` directly.
- **Files:** atomic writes (temp + fsync + `os.replace`), `fcntl.flock` for single-writer files, and tolerant readers.
- **Logging:** `logging` with a rotating file handler for the daemon. CLI diagnostics go to stderr. `--json` output is always clean JSON on stdout.
- **CLI:** `argparse`. Custom pre-parsing for `ccs --<flag> …` passthrough (ADR-0006). Exit codes: 0 ok, 1 error, 2 usage error, and the launcher mirrors claude's exit code.
- **Statusline script:**
  - Generated, self-contained, stdlib only.
  - The `statusLine.command` is `<abs python> -S -E <config_dir>/ccs-statusline.py`, where `<abs python>` is `sys.executable` of the pipx venv at generation time. The same value is used by `apply` (ADR-0016 path) and by the launcher's `--settings` injection (ADR-0006).
  - Performance budget: **p95 < 60 ms** on Apple silicon.

## Rules for implementers
- Adding a runtime dependency requires a new ADR.
- Every module with logic ships tests in the same plan. Plans aren't done until `make test lint` passes.
