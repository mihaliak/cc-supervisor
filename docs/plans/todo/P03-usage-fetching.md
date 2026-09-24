# P03: Usage fetching & normalization

- Status: todo
- Milestone: M1
- Depends on: P00 (S2 fixtures and shapes), P02
- ADRs: [0002](../../decisions/0002-usage-data-source.md), [0003](../../decisions/0003-authentication.md), [0005](../../decisions/0005-state-and-ipc.md), [0008](../../decisions/0008-limit-policy.md), [0013](../../decisions/0013-python-engineering.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
- Reliable per-profile usage data. Probe Claude Code's `get_usage`, normalize it into `UsageSnapshot`, merge in statusline live reports, and persist to `usage/<profile>.json`.
- Expose it via `ccs usage`.

## Scope
- `ccs/claude_cli.py`: locate `claude`, async subprocess helpers, the `get_usage` probe.
- `ccs/usage/normalize.py` (pure), `ccs/usage/source_claude.py` (IO), `ccs/usage/merge.py` (pure), `ccs/usage/model.py` (dataclasses).
- `schema/usage-snapshot.schema.json`, `schema/live-report.schema.json`.
- CLI `ccs usage [--profile <id>] [--refresh] [--json]`.
- Fake claude `get_usage` scenarios.

## Out of scope
- Poll scheduling and cadence (P04).
- Writing live reports (the statusline, P07). This plan only defines and reads their format.
- Policy decisions (P06).

## Design

### claude_cli.py
- `resolve_claude(cfg) -> str`:
  1. `cfg.claude_path` when set
  2. otherwise `shutil.which("claude")` on the current PATH
  3. otherwise `~/.local/bin/claude`
  - Raises `ClaudeNotFound`.
- `profile_env(profile) -> dict`: `os.environ` copy plus `CLAUDE_CONFIG_DIR=<expanded abs>`. It strips `CLAUDECODE`, `CLAUDE_CODE_*`, and `CCS_WRAPPER_ID` so probes don't inherit the parent session's context.
- `async run(argv, *, env, cwd=None, timeout, stdin_data=None) -> Completed(rc, stdout, stderr, duration)`: asyncio subprocess. On timeout it kills the process group and raises `ClaudeTimeout`.
- `async probe_usage(claude, profile, *, timeout=15.0) -> ProbeResult(raw: dict | None, error_kind: str | None, error: str | None, duration)`:
  - argv: `[claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--settings", '{"disableAllHooks":true}']`
  - `request_id = f"ccs-{uuid4().hex[:12]}"`. Write the single `control_request` line from ADR-0002 and keep stdin open until the matching `control_response` arrives. Read stdout lines with an overall deadline.
  - Ignore non-JSON lines and other message types. On a `control_response` with a matching `request_id`:
    - `subtype == "success"` → `raw = response.response`
    - otherwise → `error_kind = "source_error"`, `error = response.error`
  - `finally`: **close stdin (EOF) and wait up to 5 s for a clean exit** (P00-S2: rc 0 after ~0.8 s). Only then `terminate()`, then `kill()` after 2 s. Always reap.
  - **Never SIGKILL first.** A killed probe leaves Claude's pid registry files `<config_dir>/sessions/<pid>.json` and `<pid>.<hash>.key` behind (P00-S2). After a SIGKILL fallback, remove exactly those two files for that pid.
  - Stderr is captured (bounded to 64 KB) for classification.
- `async agents_json(claude, profile) -> list[dict]` and `async auth_status(claude, profile) -> dict`: thin wrappers, declared here for P04/P06/P09 to use. Tests use the fake.

### Error classification (`source_claude.classify`)
- **P00-S2 finding:** a logged-out profile does **not** error. `get_usage` returns `subtype: success` with `rate_limits_available: false`, `rate_limits: null`, `subscription_type: null` (fixture `logged_out.json`). This is the same shape as API-key or no-subscription accounts.
- So when `rate_limits_available == false` or `rate_limits == null`, call `auth_status` (`claude auth status --json`; rc 1 and `loggedIn: false` when logged out, per P00-S4):
  - `loggedIn == false` → `needs_sign_in`
  - otherwise → `no_subscription`
- `needs_sign_in` also applies when the error text or stderr mentions "not logged in"/"401" (defensive).
- `source_error`:
  - timeout
  - non-zero exit before a response
  - a malformed payload (required fields missing)
  - an unexpected `subtype`
- `ok`: otherwise.

### model.py (dataclasses, `to_dict`/`from_dict`, `schema: 1`)
- `Window(percent: int, resets_at: datetime | None, observed_at: datetime, source: "get_usage" | "statusline")`
- `ScopedWindow(name: str, percent: int, resets_at: datetime | None, observed_at: datetime)`
- `ExtraUsage(enabled, percent, used: Decimal | float, limit: float | None, currency: str | None, disabled_reason: str | None)`
- `UsageSnapshot(profile_id, status, error, fetched_at, polled_at, subscription_type, session: Window | None, weekly: Window | None, model_scoped: list[ScopedWindow], extra_usage: ExtraUsage | None)`
- It serializes to exactly the ADR-0005 shape: `windows.session`, `windows.weekly`, `windows.model_scoped`.

### normalize.py (pure)
- `normalize(raw: dict, *, profile_id, fetched_at) -> UsageSnapshot`:
  - `rate_limits.five_hour` → `session`, `seven_day` → `weekly`. Missing or `null` → `None`.
  - `percent = round(utilization)`, clamped 0–100 (clamping above 100 is allowed only for extra usage).
  - `resets_at`: ISO 8601 with offset → aware UTC, **truncated to whole seconds**. P00-S2 saw sub-second jitter between calls for the same window: `…20:00:00.326066` vs `…20:00:00.742595`. `null` → `None`.
  - Helper `window_key_time(dt) -> str`: `dt` rounded to the nearest minute, as ISO `YYYY-MM-DDTHH:MMZ`. P06 uses it for every window-instance key and for "resets_at advanced" comparisons, so jitter can never create a new instance.
  - `model_scoped`:
    - Primary: `rate_limits.model_scoped[]` (`display_name`, `utilization`, `resets_at`).
    - Fallback when absent: `rate_limits.limits[]` where `kind == "weekly_scoped"` and `scope.model.display_name` is set (`percent`, `resets_at`).
    - Never both; dedupe by name.
  - `extra_usage`:
    - `enabled = is_enabled`
    - `dp = decimal_places` (default 2)
    - `used = used_credits / 10**dp`, `limit = monthly_limit / 10**dp` (or `None`)
    - `percent = round(utilization)` if present, else `round(used / limit * 100)`
    - `currency`, `disabled_reason`
    - Object missing → `None`.
  - `subscription_type` copied.
  - Status `ok` when `rate_limits_available` is true.
- **Window activity helper:** `session_window_active(snapshot, now) -> bool`, true only if `resets_at` is not None and is later than `now`. Per P00-S2, both a `null` `resets_at` and a past one mean inactive. Used by P08.
- `snapshot_from_error(profile_id, status, error, previous: UsageSnapshot | None, now)`:
  - Keeps the previous windows (so displays don't blank on a transient error).
  - Sets `status` and `error`.
  - Keeps `fetched_at` as the last success. `polled_at` is always set to the attempt time.

### merge.py (pure)
- `LiveReport` (from `live/*.json`). **P03 owns this format** and `schema/live-report.schema.json` (ADR-0005). P07's statusline writes to it.
  - `{schema: 1, profile_id, wrapper_id | null, session_id, model_id, effort | null, cwd, observed_at (ISO), rate_limits: {five_hour?: {percent, resets_at}, seven_day?: {…}}}`
  - The parser is tolerant: it accepts `percent` or the raw statusline `used_percentage`, and `resets_at` as ISO 8601 or epoch seconds. Unknown keys are ignored.
- `merge(snapshot: UsageSnapshot | None, reports: list[LiveReport], *, now) -> UsageSnapshot`:
  - For `session` and `weekly` only: take the candidate with the newest `observed_at` among the snapshot window and the matching reports. A candidate from a report gets `source = "statusline"`.
  - Reports older than 10 min are ignored.
  - `model_scoped` and `extra_usage` always come from the snapshot.
- `apply_staleness(snapshot, now) -> UsageSnapshot`: if `status == ok` and `max(fetched_at, newest observed_at) < now - 10 min`, then `status = stale`.

### source_claude.py (IO)
- `async fetch_snapshot(cfg, profile, *, previous, clock) -> UsageSnapshot`: `probe_usage`, then `classify`, then `normalize` or `snapshot_from_error`.
- `write_snapshot(snapshot)`: `atomic_write_json(usage_file(profile_id), snapshot.to_dict())`.
- `read_snapshot(profile_id) -> UsageSnapshot | None` (tolerant).

### CLI `ccs usage`
- Without `--refresh`:
  - Read `usage/<id>.json` for one profile or all profiles, then `merge` with the live reports and `apply_staleness`.
  - Human output per profile: `💼 Work  session 45% → 20:00 (in 2h 13m) · weekly 50% → Sat 08:00 · Fable 4% · extra off`. Built from `timefmt`.
- `--refresh`:
  - If the daemon socket answers `hello`, send `{"op":"refresh","profile_id":…}` and wait (≤ 20 s) for the file's `polled_at` to change, then report its status.
  - Otherwise, probe directly and print the result (human or `--json`) **without writing** `usage/*.json`. The daemon is the single writer (ADR-0006, P04). The output is marked `"source": "direct_probe"`.
- `--json`: `{"profiles": [<UsageSnapshot dict>…]}`.
- No data and no daemon → `status: "no_data"` in the JSON, with a hint `run: ccs usage --refresh`.

### Fake claude scenarios (`python/tests/fake_claude/`)
- `get_usage.mode`, one of:
  - `ok` (returns the `get_usage.payload` file content, default the S2 `ok_max.json` fixture)
  - `logged_out` (the error shape from S2)
  - `malformed` (`{"rate_limits": {"five_hour": "x"}}`)
  - `slow` (sleep `get_usage.delay_s` before responding)
  - `no_response` (never answers)
  - `exit_early` (exit 1 immediately)
- It emits the `system` hook lines only when `--settings` lacks `disableAllHooks` (to assert the flag is passed).

## Tasks
- [ ] `usage/model.py` dataclasses plus `to_dict`/`from_dict` (ISO with `Z`).
- [ ] `usage/normalize.py` plus `session_window_active` and `snapshot_from_error`.
- [ ] `usage/merge.py` (`LiveReport` parsing tolerant of missing keys, `merge`, `apply_staleness`).
- [ ] `claude_cli.py`: `resolve_claude`, `profile_env`, `run`, `probe_usage`, and the `agents_json`/`auth_status` wrappers.
- [ ] `usage/source_claude.py`: `classify`, `fetch_snapshot`, `write_snapshot`, `read_snapshot`.
- [ ] `schema/usage-snapshot.schema.json` and `schema/live-report.schema.json`.
- [ ] Extend the fake claude with the `get_usage` modes and the hook-line emission.
- [ ] CLI `ccs usage` (read, `--refresh` via daemon, or a direct probe that prints but doesn't write, `--json`, human format).
- [ ] Copy or confirm the P00-S2 fixtures in `python/tests/fixtures/get_usage/`. Add `limits_only_scoped.json`, a hand-derived variant with `model_scoped` removed, to test the fallback.

## Tests
- `test_normalize.py`, per fixture:
  - `ok_max.json` → session 1/weekly 50/Fable 4/extra disabled €10.00, `disabled_reason=out_of_credits`
  - `ok_no_window.json` → `session_window_active == False`
  - `limits_only_scoped.json` → Fable from `limits[]`
  - utilization float rounding
  - `decimal_places` 0 and 2
- `test_merge.py`:
  - a newer live report beats the snapshot for session/weekly only
  - a stale report is ignored
  - `model_scoped`/`extra` untouched
  - staleness transitions at exactly 10 min
- `test_probe.py` (fake claude):
  - `ok` → raw returned and the `disableAllHooks` flag present in the call log
  - `CLAUDE_CONFIG_DIR` set and `CLAUDECODE` stripped
  - `slow` beyond the timeout → `source_error` and the process reaped (no zombie: check `os.waitpid` / psutil-free via `/bin/ps -p`)
  - `logged_out` → `needs_sign_in`
  - `malformed` → `source_error`
  - `exit_early` → `source_error`
- `test_cli_usage.py`: `--json` with no file → `no_data`; a direct `--refresh` with the fake (no daemon) → output matches the fake payload, and `usage/*.json` is **not** created.
- `@pytest.mark.live test_live_probe.py`: a real `claude` probe against `~/.claude` returns `status in {ok, no_subscription}` within 5 s (run only with `make test-live`).

## Manual pages to update
- `05-ccs-cli.md`: `ccs usage` (flags, output, JSON shape pointer).
- `07-limits-and-supervisor.md`: where usage numbers come from (Claude Code login, polling plus live statusline data, staleness).
- `11-troubleshooting.md`: `needs_sign_in`, `source_error` (Claude Code updated the experimental API), `stale`.

## Done when
- [ ] `make test lint` passes.
- [ ] `ccs usage --refresh --json` against the real `~/.claude` returns a correct snapshot (manual check, matching `/usage` in Claude Code).
- [ ] Probes never leave child processes behind (verified by the test).
- [ ] The manual pages above are updated.

## Risks & mitigations
- **`get_usage` shape changes (experimental):** all mapping lives in `normalize.py` with fixture tests. `source_error` plus the previous data are kept, never zeros.
- **The probe hangs if claude waits for more stdin:** a hard deadline plus kill of the process group.
- **The logged-out error text is unstable:** combine the text matcher with an `auth status` confirmation (P09 wrapper) before marking `needs_sign_in`.
- **Env leakage from a parent Claude session** (e.g. running inside Claude Code): strip `CLAUDECODE`/`CLAUDE_CODE_*` in `profile_env`.
