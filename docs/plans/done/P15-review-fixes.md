# P15: Review fixes

- Status: done
- Milestone: M3
- Depends on: P13
- ADRs: [0022](../../decisions/0022-review-hardening.md) (amends [0002](../../decisions/0002-usage-data-source.md), [0004](../../decisions/0004-config-and-profiles.md), [0006](../../decisions/0006-process-model.md), [0007](../../decisions/0007-pause-resume.md), [0008](../../decisions/0008-limit-policy.md), [0011](../../decisions/0011-macos-app.md)), [0005](../../decisions/0005-state-and-ipc.md), [0009](../../decisions/0009-display-conventions.md), [0013](../../decisions/0013-python-engineering.md)

## Goal
Fix every bug and security issue found by the 2026-09-25 full review.

## Decisions
- ADR-0022 records every behavior change. Fixes that only make code match an existing ADR need no new decision.

## Scope
All findings from the review, grouped by owning area below.

## Out of scope
- New features. P14 migration.

## Design
Each area owns a separate set of files, so the fixes can run in parallel. Each fix gets a regression test.

## Tasks

### Supervisor, usage, warm-up, config
- [x] A session started anyway is not paused by a model-scoped hold once its model becomes known.
- [x] Validator regexes match the whole string (ASCII only); the scheduler catches errors per profile.
- [x] A session-scoped manual hold is released when its session ends.
- [x] Live merge: within the same window instance, the higher percent wins.
- [x] The `extra_usage` instance is keyed by cap plus an arm counter, not the month.
- [x] Warm-up prompt: reject control chars and a leading `-`; `_run_locked` always finishes the attempt.
- [x] The `pause --session` hint points at `resume --session`.
- [x] Schema and validator agree; intervals ≤ 240; absolute `claude_path`/`ccs_path`; no control chars in paths/names.
- [x] `profile remove` reverts an applied statusline first.

### Daemon
- [x] Reject invalid `wrapper_id`s; unlink only known sessions.
- [x] Bound pids; the reaper survives bad records; session loading at startup survives bad files.
- [x] Config read errors (encoding, permissions) become `config.invalid`, not a crash.
- [x] Sanitize socket strings; the writer, snapshot loop, and event dedupe survive encode errors.
- [x] Abort the transport of a client that stops reading.
- [x] `status`/`refresh`/`warmup` reload the config for an unknown profile.
- [x] The daemon waits for the lock under launchd; the plist passes `CCS_STATE_DIR`; `ccs_executable` prefers the running entrypoint.
- [x] Sessions loaded from disk must re-register within a grace period.
- [x] Atomic writes follow symlinks instead of replacing them.

### Launcher
- [x] Ack `was_busy: null` on `unknown`; no ESC after an override; no resume prompt after typing while paused.
- [x] Restore and replay terminal modes on suspend and exit.
- [x] Launcher logging never writes to the TTY; `claude_cli.run` maps every exec `OSError`.
- [x] Submit detection counts only a bare `\r` outside pastes, across split reads.
- [x] The override report is marked sent only on success and is resent after re-register.
- [x] Ctrl-C at the start prompt means "no" and exits 130 without a traceback.
- [x] Raw mode uses `TCSANOW` (typeahead kept); Ctrl-Z is stripped only outside pastes.
- [x] Clients refuse a socket or state dir not owned by the user.
- [x] Keychain service name uses the resolved default-dir check.

### Statusline, doctor, scripts
- [x] Revert uses the recorded `settings_path`; apply on a new dir reverts the old one first; uninstall reverts from bookkeeping and keeps unreverted records.
- [x] Strip control chars from stdin-derived statusline text; validate `session_id`/`wrapper_id` before building paths.
- [x] The doctor handles shebangs with spaces, flags a missing applied script, warns on model-scoped pauses without the statusline, and warns on a non-default state dir.
- [x] Re-apply keeps the user's extra statusLine keys; missing bookkeeping records `previous = None`; settings are written before bookkeeping; the CLI catches `ValueError`.
- [x] Newlines can't escape the generated script's comment; `parse_time` survives overflow.

### macOS app
- [x] Reset the request id on every connect.
- [x] Resolve state and config dirs from the LaunchAgent env.
- [x] Cancel with SIGINT, then SIGTERM, then SIGKILL; enter the reader group before `run()`.
- [x] Config writes take `.config.lock`.
- [x] The sign-in deep link asks for confirmation.
- [x] Save pending edits on quit; keep edits across profile remove; `load()` clears `conflict`.

## Tests
- A regression test for each task (pytest; Swift unit tests where feasible).
- `make test lint` passes.

## Manual pages to update
- 01-installation, 05-ccs-cli, 06-statusline, 07-limits-and-supervisor, 08-warm-up, 10-configuration-reference, 11-troubleshooting, 04-menu-bar-app (only where behavior changed).

## Done when
- Every task is ticked, `make test lint` passes, and the manual pages are updated.

## Risks
- Pause/resume changes can't be fully checked headless (terminal-mode replay in a real terminal).

## Result
- Shipped every task above. Each fix has a regression test, mostly in `python/tests/test_p15_{daemon,launcher,core}.py`, the statusline/doctor/install test files, and the new `macos/Tests/{ProcessRunner,LaunchAgentEnvironment,AppModel}Tests.swift`. `make test lint`: 1139 passed, 1 skipped; Swift tests, ruff and mypy are green.
- Deviations and additions:
  - `version` and `profiles` are now required by both the validator and the schema.
  - `supervisor/<id>.json` gains `extra_usage_arm`, and legacy `extra_usage:<YYYY-MM>:<cap>` keys are upgraded to arm 0 on load. Session supervision gains `pending_override_instances`. Both schemas are updated.
  - The LaunchAgent runs `ccs daemon run --launchd`. `make upgrade` re-runs `ccs daemon install`, so existing installs pick up the new plist.
  - Launcher diagnostics go to `logs/launcher.log`. New `launcher/term_modes.py` tracks terminal modes.
  - The app's Swift client applies the same socket-ownership check as the Python clients.
  - Adding a profile in the app also keeps pending edits. A `CommitTextField` commits when it disappears.
- Added after the first install: a warm-up whose window showed up late in the usage API was reported as `window_not_started`. Seen on 2026-09-25 with an auto-chain for `personal`: polls stayed without a window for at least 66 s. Warm-up confirmation now re-polls for about 3 minutes (ADR-0022).
- Found during the install: `ccs daemon install` ran `bootstrap` right after `bootout`, while the old daemon was still exiting, and failed with `5: Input/output error`. That left the daemon unloaded. `bootstrap` is now retried up to 15 times, 1 s apart.
- Follow-ups:
  - Terminal-mode reset and replay on Ctrl-Z/`fg`, and draft protection, still need a manual check in Ghostty and Terminal.app (headless tests only).
  - Optional: share the id pattern through a tiny stdlib module embedded in the statusline script. Today a parity test keeps `runtime.ID_RE` and `paths.is_valid_id` equal.
  - Optional: add example `state.dir` / `statusline.model_scoped` entries to `schema/fixtures/ccs/doctor.json`.
