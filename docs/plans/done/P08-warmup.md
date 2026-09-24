# P08: Warm-up scheduler

- Status: done
- Milestone: M1
- Depends on: P00 (S5: warm-up starts a window, inactive-window shape), P04 (daemon loop, `poller.request_poll`, events, snapshot builder, socket ops)
- Soft dependency: P06 holds and session activity (rules 4 and 7). If P06 isn't done yet, they read empty lists through interfaces; the integration tests come with P06.
- ADRs: [0010](../../decisions/0010-warmup.md), [0005](../../decisions/0005-state-and-ipc.md), [0006](../../decisions/0006-process-model.md), [0008](../../decisions/0008-limit-policy.md), [0013](../../decisions/0013-python-engineering.md), [0015](../../decisions/0015-notifications.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
Start each profile's 5-hour session window as early as useful, automatically:
- at scheduled times
- at app start
- at unlock or wake
- right after a window resets during active hours (auto-chain)

The daemon owns all of it: rules, scheduling, execution, and events.

## Scope
- `ccs/warmup/rules.py`: pure skip-rule evaluation (ADR-0010 order, reasons).
- `ccs/warmup/runner.py`: executes a warm-up, confirms the window, emits events.
- `ccs/daemon/scheduler.py`: schedule and auto-chain timers, missed-schedule catch-up, clock-jump detection, per-profile lock and cooldown bookkeeping.
- State file `warmup/<profile_id>.json` and the working dir `warmup/cwd/`.
- Socket op `warmup` (ADR-0005) and CLI `ccs warmup (--profile <id> | --all) [--trigger …] [--force] [--json]`.
- Snapshot field `supervisor.next_warmup_at` (wired into P04's snapshot builder).

## Out of scope
- Detecting unlock, wake, and app start (the Swift app, P10). It only forwards triggers.
- Notification posting and notification fields (P04 `EventBus.emit` computes `title`/`body`/`notify`; P10 or the P06 fallback posts). This plan only emits events.
- The warm-up settings UI (P11).

## Design
- **P00-S5 findings:**
  - Verified on a profile with an active window: `claude -p "Reply with just: ok" --model haiku --no-session-persistence --settings '{"disableAllHooks":true}'` with `stdin=DEVNULL` exits 0 in ~3.4 s and prints `ok`.
  - No transcript or session files were persisted and there was no `~/.claude.json` bookkeeping. The usage delta was 0 %.
  - "Starts a new window" (`resets_at ≈ now+5h`) is **not yet observed** (it would have started a real window) and is deferred to the user's first real warm-up.
  - Rule 3 treats `resets_at` null/None **or** in the past as inactive (P03 `session_window_active`).

### Triggers
`manual`, `app_start`, `unlock_wake`, `schedule`, `auto_chain` (CLI default `manual`).

### `rules.py`
```python
WarmupContext(profile, trigger, now, snapshot, sessions, holds, last_attempt_at, force)
Decision(run: bool, reason: str | None)
```
Evaluated in order; the first match skips:
1. `disabled`: `warmup.enabled` is false, or the trigger toggle is off (`schedule` → `triggers.schedule` non-empty; `app_start`; `unlock_wake`; `auto_chain`). `manual` has no toggle.
2. `needs_sign_in`: `snapshot.status == "needs_sign_in"`.
3. `window_active`: `snapshot.windows.session.resets_at > now`. The inactive shape (null or past) is confirmed by P00-S5.
4. `session_busy`: any supervised session of the profile has `activity != "idle"` and `!= "unknown"`.
5. `cooldown`: `now - last_attempt_at < cooldown_minutes`.
6. `outside_active_hours`: only for `auto_chain`, and for `schedule` catch-up runs. Supports windows that wrap past midnight (`start > end`).
7. `weekly_hold`: the profile has an active `weekly` hold.

`force=True` skips rules 3–7, never 1–2.

### `runner.py`
- `run_warmup(profile, trigger, deps) -> WarmupOutcome`, guarded by a per-profile `asyncio.Lock`. If the lock is busy, return skipped with reason `in_progress`.
- Command (argv list, no shell):
  ```
  [claude_path, "-p", prompt, "--model", model, "--no-session-persistence", "--settings", '{"disableAllHooks":true}']
  ```
- Environment: the daemon env plus `CLAUDE_CONFIG_DIR=<abs config_dir>`, with all `CCS_*` variables stripped.
- `cwd` = `warmup/cwd/` (created if missing); `stdin` = DEVNULL.
- asyncio subprocess with timeout 120 s. On timeout, kill the process group and fail with reason `timeout`.
- A non-zero exit fails with reason `exit_<code>` and the stderr tail (≤ 500 chars, trimmed).
- On exit 0, `await poller.request_poll(profile_id)` (P04 forced-poll API; awaits the coalesced probe).
  - `succeeded` if `windows.session.resets_at > now`
  - otherwise `failed` with reason `window_not_started`
- Events (ADR-0005 / 0015):
  - `warmup.started`: `data: {trigger}` (not a notified type)
  - `warmup.skipped`: `data: {trigger, reason}` (not a notified type)
  - `warmup.succeeded`: `data: {trigger, resets_at}`
  - `warmup.failed`: `data: {trigger, reason}`
  - Emitted via `EventBus.emit` only. `title`/`body`/`notify` are computed there (P04), with the `config.notifications.warmup` toggle. Template examples for `events.notification_text` (P06): succeeded → title `💼 Work`, body `Session window started, resets 11:02`; failed → body `Warm-up failed: <reason>`.
- Event `key`: `warmup:<profile>:<attempt_started_at>`.

### State `warmup/<profile_id>.json`
```jsonc
{"schema":1,"profile_id",
 "last_attempt":{"at","trigger","result":"succeeded|failed|skipped","reason","resets_at"},
 "last_success_at","next_scheduled_at","next_trigger":"schedule|auto_chain|null",
 "history":[ …last 20 attempts (not skips)… ]}
```
- Skips update only a separate `last_skip` field: `{at, trigger, reason}`.
- Cooldown counts only real attempts, not skips.

### `scheduler.py`
- Tick every 30 s. Keep `last_tick`.
- **Scheduled times:** for each profile, compute the next occurrence of each `{time, weekdays}` in `clock.local_tz()` (P02; an IANA zone, so DST-aware).
  - DST gap (nonexistent local time): run at the first valid minute after.
  - DST fold (ambiguous time): first occurrence only (`fold=0`).
  - When `now >= occurrence`, evaluate with trigger `schedule` and mark that occurrence consumed (in memory plus `warmup/<profile>.json`), so it runs once.
- **Clock jump / sleep:** if `now - last_tick > 60 s` (2× tick), treat it as a wake. Run a missed-schedule catch-up:
  - for each profile, the latest consumed-not-run occurrence between `last_tick` and `now`, on the **same local day**
  - evaluated with trigger `schedule` plus the extra active-hours check (rule 6 applies to catch-up)
  - at most once per profile per wake
- **`unlock_wake` op from the app:** run the same missed-schedule catch-up first, then evaluate `unlock_wake`. After a success, the second evaluation skips on `window_active`.
- **Auto-chain:** when a profile has `auto_chain` on and a known `windows.session.resets_at`, arm a timer at `resets_at + 15 s`. On fire:
  1. Force a poll.
  2. If the reset is not confirmed (same `resets_at` still in the past, or status not ok), retry every 30 s for up to 10 min (mirrors ADR-0008).
  3. Once confirmed, evaluate with trigger `auto_chain`.
  4. Re-arm whenever the snapshot's `resets_at` changes.
- If P06 already performs reset confirmation for held profiles, subscribe to its internal `session_reset_confirmed(profile_id)` signal instead of duplicating polls. Document which one is used in `## Result`.
- `next_warmup_at(profile_id)`: `min(next schedule occurrence, next auto-chain opportunity)`. The auto-chain opportunity is `resets_at + 15 s` when it falls inside active hours. The value is used by the snapshot builder and written to `warmup/<profile>.json`.
- Config reload (P04 hook): recompute all occurrences and timers.

### Socket op `warmup`
- Request: `{profile_id? | all: true, trigger, force?}`.
- Evaluates the rules synchronously and returns right away:
  `{"results":[{"profile_id","decision":"started|skipped","reason"}]}`.
- Started runs continue in the background, and their outcome arrives as events.

### CLI `ccs warmup`
- Requires the daemon: if it is not running, exit 1 with the hint `ccs daemon start`.
- Human output: one line per profile, e.g. `💼 work: started (window will be confirmed; see ccs events --follow)` or `🏠 personal: skipped (window_active, resets 20:00)`.
- `--json` prints the op result verbatim.

## Tasks
- [x] `ccs/warmup/rules.py`: `WarmupContext`, `Decision`, `evaluate(ctx)`, `in_active_hours(now, start, end)` (wrap-around).
- [x] `ccs/warmup/runner.py`: `build_command(profile, claude_path)`, `run_warmup(...)`, outcome → events + state update.
- [x] Warm-up state store: read/write `warmup/<profile_id>.json` via `fsio`, history capped at 20.
- [x] `ccs/daemon/scheduler.py`: occurrence calculator (weekdays, DST), tick loop, clock-jump detection, catch-up, auto-chain timers + confirmation retries, `next_warmup_at`.
- [x] Wire the scheduler into the P04 daemon loop and its config-reload hook.
- [x] Socket op `warmup` handler (replace the P04 stub if one exists).
- [x] CLI `ccs warmup` subcommand (argparse), human and `--json` output.
- [x] Snapshot builder: fill `supervisor.next_warmup_at` from the scheduler.
- [x] fake_claude: `-p` scenario support (exit code, delay, whether the next `get_usage` shows an active window).

## Tests
- **`rules.py` table:** each rule firing in isolation, the precedence order, `force` bypassing 3–7 only, `manual` without a toggle, active hours with wrap-around and edges (start inclusive, end exclusive).
- **Scheduler with a fake clock and a fixed `ZoneInfo("Europe/Bratislava")`:**
  - weekday boundary (Sun 23:59 → Mon 06:00)
  - occurrence consumed once
  - DST spring-forward day 2026-03-29 with a schedule at 02:30 → runs at 03:00
  - DST fall-back day 2026-10-25 with a schedule at 02:30 → runs once
  - sleep gap 05:50 → 09:10 with a schedule at 06:00 → one catch-up if inside active hours
  - sleep across midnight → no catch-up for the previous day
- **Auto-chain:** fires at `resets_at + 15 s`, retries until the reset is confirmed, gives up after 10 min, skips outside active hours, re-arms on a `resets_at` change.
- **Runner with fake_claude:** success path (event with `resets_at`), timeout kill, non-zero exit, `window_not_started`, concurrent calls → `in_progress`, env contains `CLAUDE_CONFIG_DIR` and no `CCS_*`.
- **Socket op + CLI:** `--all` mixed decisions, daemon-down error, `--json` shape.

## Manual pages to update
- `08-warm-up.md` (triggers, skip reasons, schedule semantics, DST and sleep behavior, defaults)
- `05-ccs-cli.md` (`ccs warmup`)
- `09-notifications.md` (warm-up notifications)
- `10-configuration-reference.md` (`warmup.*`)

## Done when
- [x] All four automatic triggers plus `manual` work end-to-end against fake_claude under the daemon.
- [x] Skip reasons match ADR-0010 order. `--force` semantics are verified.
- [x] `warmup/<profile>.json` and `supervisor.next_warmup_at` are populated.
- [x] `make test lint` passes.
- [x] Listed manual pages describe the shipped behavior. The `> Status:` lines are left for the orchestrator to flip once the other contributing plans ship.

## Risks & mitigations
- **The inactive-window shape differs from the assumption** → rule 3 isolated in one function, fixture from P00-S5.
- **A warm-up burns meaningful usage** (a wrong model) → default `haiku`, prompt `Reply with just: ok`, and the model is shown in the settings UI.
- **Timers are lost across daemon restarts** → occurrences are recomputed from config on start, and consumed occurrences are persisted.
- **Unlock storms** → per-profile lock, cooldown, and the app-side 60 s debounce (P10).

## Result
- **Shipped:**
  - `ccs/warmup/rules.py`: `TRIGGERS`, reason constants, `WarmupContext`, `Decision`, `evaluate`, `in_active_hours`, `parse_hhmm`, `trigger_enabled`, `session_busy`.
  - `ccs/warmup/state.py`: `WarmupStore`, the write-through cache of `warmup/<profile>.json`.
  - `ccs/warmup/runner.py`: `WarmupRunner`, `build_command`, `warmup_env`, `WarmupOutcome`.
  - `ccs/warmup/cli.py`: `ccs warmup`.
  - `ccs/daemon/scheduler.py`: the pure occurrence math (`resolve_local`, `occurrences_between`, `next_occurrence`), `Scheduler`, and `install(daemon, *, tick_s=30)`.
  - The scheduler is registered in `ccs.daemon.extensions.EXTENSIONS`.
  - The `warmup` socket op is documented in `schema/ipc.md`.
  - fake_claude `print.activate_window_s`: later `get_usage` payloads show an active window.
- **Tests:** `make test lint` passes (380 Python passed, 1 live skipped; Swift, ruff, mypy `--strict`). New test files:
  - `test_warmup_rules`: the rule table, precedence, `force`, active hours
  - `test_scheduler`: occurrences, the weekday boundary, both DST days, sleep gaps, auto-chain fire/retry/give-up/outside-hours/re-arm, `next_warmup_at`, the op
  - `test_warmup_runner`: success, exit code, timeout kill, `window_not_started`, `claude_not_found`, `in_progress`, env, distinct event keys
  - `test_cli_warmup`: the CLI with a daemon, daemon-down, `--json` shape, `next_warmup_at` in status and snapshot
  - `test_warmup_daemon`: `app_start`, `unlock_wake`, `schedule`, and `auto_chain` each end to end under a real in-process daemon with fake_claude
- **Live check (read-only):** real `get_usage` probes of `~/.claude` and `~/.claude-work`, fed into the pure rules. Both windows were active, so every trigger returned `window_active`. No `claude -p` was executed, and no real state or config dirs were created.
- **Deviations:**
  - **Reset confirmation for auto-chain lives in the scheduler.** P06 exposes no reset signal, so the scheduler polls at `resets_at + 15 s`, then retries every 30 s for up to 10 min. A window whose reset was confirmed or given up on is never re-armed. After a daemon restart, a reset that already passed is confirmed immediately.
  - **Event keys:** the outcome event key is `warmup:<profile>:<attempt start, microsecond ISO>`, not second precision. Two attempts in the same second must not dedupe each other's outcome (found by the daemon test). `warmup.started` and `warmup.skipped` carry no key and are never deduped or notified.
  - `last_attempt.result` is `running` while an attempt is in flight. Cooldown starts at attempt begin. A daemon killed mid-run leaves `running`; the next attempt overwrites it.
  - The state doc adds `consumed`: occurrences already run, UTC ISO, the last 50 within 3 days. Persisting it makes each occurrence run once across restarts.
  - **Daemon (re)start:** the first tick runs any unconsumed occurrence from the last 10 min as a normal `schedule` run, not a catch-up. This covers a launchd restart around a scheduled time.
  - **Race fix:** a profile is reserved synchronously when its warm-up is spawned, so the evaluation that follows sees `in_progress` (e.g. `unlock_wake` right after its catch-up) instead of reporting `started`.
  - Active hours with `start == end` mean the whole day.
  - The op reply adds `trigger` and a per-result `resets_at` (current session reset) so the CLI can print `skipped (window_active, resets 20:00)`.
  - `ccs warmup` needs the daemon: without it, exit 1 with the hint `ccs daemon start`.
  - `warmup_env` strips all `CCS_*`, in addition to `profile_env`'s parent-session stripping.
- **Integration notes:**
  - **P06, notification text:** no warm-up templates are registered here. P06 must call `events.register_text("warmup.succeeded" | "warmup.failed", …)`. The event data carries `trigger`, plus `resets_at` on success and `reason` (and optional `detail`) on failure. Until then the P04 fallback text applies.
  - **P06, holds:** rule 7 reads hold ids from both a `holds` key in `contribute_supervisor` contributions and `supervisor/<profile>.json` `holds[]`. Each entry is a string, or an object with `id` or `kind`, skipped when `"active": false`. Keep either shape so `weekly` holds are seen.
  - **P06, sessions:** rule 4 uses `daemon.sessions_for(pid)[].activity`, where `busy`, `shell`, or `waiting` counts as busy.
  - **P10, app triggers:** send `{"op":"warmup","all":true,"trigger":"app_start"}` at launch, and `"unlock_wake"` on `com.apple.screenIsUnlocked` / `NSWorkspace.didWakeNotification`, debounced 60 s. Replies are immediate; outcomes arrive as `warmup.*` events with `data.notify`. Use `supervisor.next_warmup_at` from the snapshot for display.
