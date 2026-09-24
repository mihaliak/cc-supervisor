# P06: Supervisor policy & pause/resume

- Status: todo
- Milestone: M1
- Depends on: P04, P05
- ADRs: [0007](../../decisions/0007-pause-resume.md), [0008](../../decisions/0008-limit-policy.md), [0005](../../decisions/0005-state-and-ipc.md), [0009](../../decisions/0009-display-conventions.md), [0015](../../decisions/0015-notifications.md), [0013](../../decisions/0013-python-engineering.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
- The daemon decides warn/pause/resume per ADR-0008 and drives the launchers (P05) to pause and resume ccs sessions.
- It persists holds across restarts, emits events carrying Python-built notification text, falls back to osascript notifications, and exposes `ccs pause|resume|sessions`.

## Scope
- `ccs/supervisor/{__init__,policy,engine,ledger,model}.py`: the pure policy and the IO engine.
- Notification text builders in `ccs/events.py`. `ccs/notify.py` (osascript fallback plus the dispatcher).
- Completing the daemon ops `pause`/`resume`, the `register_wrapper` reply (holds, `started_overridden`), and `wrapper_event` handling.
- The snapshot `supervisor` fields (`state`, `active_sessions`, `paused_sessions`, `other_sessions`, `resume_at`).
- CLI `ccs pause`, `ccs resume`, `ccs sessions`.

## Out of scope
- Keystroke injection mechanics (P05). Warm-ups (P08). Native notifications (P10).
- Background agents and plain sessions (display counts only, ADR-0007).

## Design

### Model (`supervisor/model.py`)
- `Hold`:
  - `id`: `session` | `weekly` | `model_scoped:<Name>` | `extra_usage` | `manual` (ADR-0005 hold ids)
  - `kind`, `scope: str | None` (the model name for `model_scoped`, the `wrapper_id` for a session-scoped `manual` hold, else `None`)
  - `instance`: the window-instance key (`session:<resets_at ISO>`, `weekly:<ISO>`, `model_scoped:<name>:<ISO>`, `extra_usage:<YYYY-MM>`, `manual:<created_at ISO>`)
  - `resets_at: datetime | None`, `created_at`
  - `confirm_started_at: datetime | None`, `next_confirm_poll_at: datetime | None`
- `ProfileSupervisorState` is persisted in `supervisor/<profile>.json`, `schema: 1`:
  - `holds: list[Hold]`
  - `warned: list[str]`: dedupe keys, bounded to 500
  - `paused_instances: list[str]`: hysteresis, bounded to 500
  - `released_instances: list[str]`: manual resumes and time-based clears, bounded to 500
  - `ledger: list[LedgerEntry]`: last 200 entries of `{ts, action: pause|resume|override|time_based_clear|manual_pause|manual_resume, instance, wrapper_ids, detail}`
- `SessionView`: `wrapper_id, model_id | None, activity, supervision: {state: running|paused|overridden, holds: [hold ids], paused_at, resume_at, was_busy_at_pause: bool | None, overridden_instances: [str]}`. `overridden_instances` is additive to ADR-0005.

### Policy (`supervisor/policy.py`, pure)
- `evaluate(profile, snapshot, state, sessions, now) -> Decision(actions, new_state, events)`.
- Actions:
  - `PauseSession(wrapper_id, hold_ids, resume_at)`
  - `ResumeSession(wrapper_id, prompt | None)`
  - `MarkRunning(wrapper_id)`: overridden → running, no injection
  - `ForcePoll(at)`
  - `LogWarning(msg)`
- Steps:
  1. **Data gate:** `fresh = snapshot is not None and snapshot.status == "ok"`. Stale, `source_error`, or `needs_sign_in` means **no new warns, holds, or pauses** (ADR-0008 stale rule). Holds still go through time-based confirmation.
  2. **Spill:** `spill_active = limits.extra_usage.spill and extra_usage and extra_usage.enabled`. When `spill_active`, no new `session`/`weekly` holds are created. Existing `session`/`weekly` holds are released (resume) with ledger `detail: "spill_active"`. Their warns still fire.
  3. **Per window**, only when `fresh`:
     - `session` uses `limits.session`, `weekly` uses `limits.weekly`, and each `model_scoped[i]` uses `limits.model_scoped`.
     - `percent >= warn` and `warn:<instance>` not in `warned` → `limit.warn` event, record the key.
     - `percent >= pause`, instance not in `paused_instances` or `released_instances`, `profile.supervisor.enabled`, and not spill-suppressed → add a `Hold`, add the instance to `paused_instances`, emit `limit.pause`.
     - Model-scoped with `warn_only: true`: no hold. At `percent >= pause`, a second `limit.warn` with `data.level = "pause_level"` (key `warn95:<instance>`).
     - A `resets_at == None` window (inactive) creates no hold.
  4. **Extra usage:** when `extra_usage.enabled`:
     - `percent >= warn` → `limit.warn` (key `warn:extra_usage:<YYYY-MM>:<limit>`), in both spill modes.
     - When `spill_active`, `percent >= pause`, and `profile.supervisor.enabled` → `extra_usage` hold (instance `extra_usage:<YYYY-MM>`, `resets_at=None`).
  5. **Reset confirmation** (per hold with `resets_at`):
     - `now >= resets_at + 15 s` and `confirm_started_at is None` → set it and `ForcePoll(now)`.
     - While confirming and `fresh` with `snapshot.observed/fetched > resets_at`: clear when the matching window's `resets_at > hold.resets_at` (advanced) **or** `percent < warn`.
     - Not cleared → `ForcePoll(now + 30 s)`.
     - `now >= confirm_started_at + 10 min` → time-based clear plus `LogWarning`, ledger `time_based_clear`, instance added to `released_instances`.
     - An `extra_usage` hold clears when `fresh and (not extra.enabled or extra.percent < pause or not spill)`.
     - A `manual` hold clears only via `ccs resume`.
  6. **Apply to sessions**, per session:
     - Applicable holds are:
       - `session`, `weekly`, `extra_usage`, profile-wide `manual` (`scope=None`), plus a `manual` hold with `scope == wrapper_id`
       - `model_scoped:<Name>` when `model_id` is not None and `name.lower()` is in `model_id.lower()`. An unknown model means not applicable.
     - `running` with applicable holds whose instances are not in `overridden_instances` → `PauseSession(hold_ids, resume_at = max resets_at of those holds, None if any is None)`.
     - `paused` with no applicable active holds → `ResumeSession(prompt = profile.supervisor.resume_prompt if was_busy_at_pause is True else None)`.
     - `overridden` when every instance in `overridden_instances` is no longer active → `MarkRunning`. A **new** hold instance on an overridden session → `PauseSession` (the override covers only the instances present when the user overrode, ADR-0007).
  7. **Events** get `data.title`/`data.body`/`data.notify` inside `EventBus.emit` (P04), from the `events.notification_text(...)` templates below. Policy only builds the event data. `limit.pause` data includes `sessions_paused` (count of `PauseSession` actions for that hold) and `resume_at`. `limit.resume` is emitted once per profile per cleared instance with `sessions_resumed`.
- Every threshold comes from `profile.limits`. There are no literals (ADR-0008 rule).

### Engine (`supervisor/engine.py`, IO)
- Registered on the P04 hooks.
  - `on_usage_updated(pid)` and `on_tick(now)` → `evaluate`, then execute the actions and persist the new state (`atomic_write_json(supervisor_file)`) **before** sending commands. The ledger records every action.
  - The tick runs only if a hold has a confirmation due or a session needs action (cheap check).
- `PauseSession`:
  1. Update the session record to `paused` (holds, `paused_at`, `resume_at`).
  2. `WrapperConn.send_cmd("pause", {holds, resume_at})`.
  3. On ack: `was_busy_at_pause = detail.was_busy`. Store `session_id` if given.
  4. Ack timeout or failure: `was_busy_at_pause = None` and a logged warning. The session stays paused (it gets no resume prompt later).
- `ResumeSession`:
  1. `send_cmd("resume", {prompt})`.
  2. On any ack: session `running`; clear `holds`, `paused_at`, `resume_at`, `was_busy_at_pause`.
  3. Ledger `resume` with the ack result.
- `MarkRunning`: update the record only.
- `ForcePoll`: `poller.request_poll(pid, at)`.
- `on_wrapper_registered(wrapper)`:
  - `started_overridden` → state `overridden`, `overridden_instances` = active hold instances.
  - Re-registration of a known `wrapper_id` → keep the persisted `supervision`. If it's `paused`, re-send `pause` so the relaunched launcher sets its local flag. Its ESC check finds idle, so there is no double interrupt.
  - The reply includes `holds` (active hold ids plus `resume_at`) and `supervision`.
- `on_wrapper_event`:
  - `input_submitted_while_paused` → session `overridden`, `overridden_instances` = the session's current hold instances.
  - Ledger `override`. `limit.override` event, which is not notified (ADR-0015).
- `on_config_changed`: re-evaluate every profile. Profiles that were removed get their holds dropped and their sessions resumed without a prompt.
- **Durability:** at daemon start, load every `supervisor/*.json` and `sessions/*.json`. Evaluate once the first snapshot arrives. Confirmation due-times are recomputed from `now`.
- **Snapshot supervisor fields:**
  - `state`: `paused` if any hold is active, else `warned` if any window ≥ its warn threshold (fresh), else `normal`.
  - `active_sessions`: count of registered wrappers.
  - `paused_sessions`: count with `paused`.
  - `other_sessions`: from the sampler.
  - `resume_at`: the latest `resets_at` among the profile's active holds. It is null when there are no holds or only `resets_at == None` holds (manual, extra usage).
  - `next_warmup_at` stays the P08 hook.

### Manual control (daemon ops, CLI)
- `pause {profile_id}` → a profile-wide `manual` hold (instance `manual:<now>`), then `evaluate`. `pause {wrapper_id}` → a `manual` hold with `scope=<wrapper_id>` (instance `manual:<wrapper_id>:<now>`).
- `resume {profile_id}`:
  - Clears every active hold of the profile.
  - Adds their instances to `released_instances`, so the same window instance never re-pauses.
  - Ledger `manual_resume`, then `evaluate`, which resumes sessions (with a prompt when `was_busy_at_pause`).
- `resume {wrapper_id}`: the session becomes `overridden` for its current instances, plus an immediate `ResumeSession` with a prompt if `was_busy_at_pause`.
- CLI:
  - `ccs pause (--profile <id> | --session <wrapper_id>) [--json]` and `ccs resume (…)`. They need the daemon; without it they exit 1 with `supervisor not running`.
  - `ccs sessions [--profile <id>] [--json]` lists supervised sessions (wrapper_id short, profile, cwd, model, activity, supervision state, `resume_at`) plus the other-session counts per profile. It comes from the `status` op, falling back to the files.

### Notification text (`events.notification_text(type, profile, data, now) -> (title, body)`, pure)
- Templates per ADR-0015. Examples:
  - `limit.warn`:
    - title `💼 Work: session at 82%`
    - body `Resets 20:00 (in 1h 12m)`
    - weekly title `… weekly limit at 86%`
    - model title `… Fable weekly limit at 82%`
    - extra title `… extra usage at 80% (€8.00 / €10.00)`
    - `pause_level` title variant `… Fable at 95% (not pausing)`
  - `limit.pause`:
    - title `💼 Work paused at 90%`
    - body `2 sessions paused. Resumes 20:00 (in 42m)`
    - with 0 sessions: `New ccs sessions will ask before starting. Resumes …`
    - an unknown resume time gives `Resumes when credits allow`
  - `limit.resume`: title `💼 Work resumed`, body `2 sessions continued`.
  - Also the P04 error types: `auth.required` (title `💼 Work: sign in required`, body `Open CC Supervisor → Settings → Profiles → Work → Sign in, or run: ccs auth login --profile work`), `usage.source_error`, `config.invalid`.
  - Also the P08 warm-up types `warmup.succeeded`/`warmup.failed` (templates from P08's examples).
- Times go through `timefmt`.

### Dispatcher and fallback (`notify.py`)
- An EventBus subscriber. `EventBus.emit` (P04) has already computed `title`/`body`/`notify`, applied the toggles, and deduped. The dispatcher computes nothing.
- For events with `data.notify == true`: if **no connected subscriber has `client == "app"`**, it runs `osascript -e 'display notification "<body>" with title "<title>"'` (AppleScript-escaped, async, 5 s timeout, errors logged). Otherwise it does nothing, because the app posts (P10, ADR-0015).
- Error types are limited to 1 per hour per profile and type by the hour-bucket event keys from P04 (`auth:<pid>:<YYYYmmddHH>`), which dedupe drops.

## Tasks
- [ ] `supervisor/model.py` plus JSON (de)serialization and `schema/supervisor-state.schema.json`.
- [ ] `supervisor/policy.py`: `evaluate`, implementing steps 1–7.
- [ ] `events.notification_text`: replace P04's minimal fallback with templates for all notified types (limit.*, warmup.*, auth.required, usage.source_error, config.invalid). `EventBus.emit` already calls it.
- [ ] `supervisor/ledger.py`: bounded append and pruning helpers.
- [ ] `supervisor/engine.py`: hook registration, action execution, persistence-before-send, acks, durability load, snapshot supervisor fields.
- [ ] Daemon op handlers `pause`/`resume` (replace the P04 `not_implemented`). The `register_wrapper` reply and `wrapper_event` handling.
- [ ] `notify.py`: osascript fallback for `notify == true` events when no app subscriber is connected.
- [ ] CLI `ccs pause`, `ccs resume`, `ccs sessions`.
- [ ] Session record writes include `overridden_instances` (update `schema/` session schema).

## Tests
- `test_policy_table.py`: table-driven with FakeClock and synthetic snapshots. Each row has input percent, config, spill/warn_only, and state → expected actions and events. It covers:
  - session 79/80/89/90 (warn at 80, pause at 90), weekly 94/95
  - model-scoped `warn_only` false (only matching-model sessions paused; a non-Fable session untouched) and true (two warns, no pause)
  - spill on + credits enabled: session 95 → no pause, warn fires; extra 90 → pause
  - spill on + credits disabled (`out_of_credits`) → normal pause
  - spill turning on while a session hold is active → release
  - hysteresis: the same instance never re-pauses after resume, override, or manual resume; a new `resets_at` re-arms
  - stale, `source_error`, `needs_sign_in` → no new holds; the existing hold time-clears after `+15 s + 10 min`
  - reset confirmation: advanced `resets_at` clears; `percent < warn` clears; neither → ForcePoll every 30 s
  - `was_busy_at_pause` true/false/None → resume prompt present/absent/absent
  - an overridden session with a new instance → paused again
  - an unknown `model_id` → a model-scoped hold is not applicable
  - `supervisor.enabled=false` → warns only
- `test_notification_text.py`: golden strings for every template, with a fixed clock and TZ.
- `test_engine_integration.py` (in-process daemon, fake claude, fake launcher clients speaking the P04 protocol and auto-acking):
  - usage crosses 90 → `pause` cmd to both wrappers; ack `was_busy` true/false stored
  - reset + confirming poll → `resume` with the prompt only to the busy-at-pause one
  - `input_submitted_while_paused` → overridden, no resume cmd later
  - manual `ccs pause/resume --profile`
  - `register_wrapper` with `started_overridden`
- `test_durability.py`:
  - create a hold, stop the daemon, restart → hold loaded
  - the reconnecting wrapper gets `pause` re-sent
  - confirmation proceeds and the resume happens once
- `test_notify.py`: `notify == true` and no app subscriber → osascript runner called with escaped text; app subscribed → not called; `notify == false` → not called.
- `test_notification_text.py`: golden title/body per type (incl. the `auth.required` text and a 0-session pause).

## Manual pages to update
- `07-limits-and-supervisor.md`: thresholds table, warn/pause/resume lifecycle, what "paused" means, overrides, weekly, Fable toggle, spill toggle, manual pause/resume, stale-data behavior.
- `09-notifications.md`: every notification, its toggle, fallback behavior without the app.
- `05-ccs-cli.md`: `ccs pause`, `ccs resume`, `ccs sessions`.
- `10-configuration-reference.md`: `limits.*`, `supervisor.*`, `notifications.*` semantics (cross-check with P02).

## Done when
- [ ] `make test lint` passes, and policy tests cover every ADR-0008 table row.
- [ ] Live check on a real profile: a temporarily lowered `limits.session.pause` (e.g. to the current percent) pauses a busy `ccs` session (ESC interrupt, statusline ⏸, notification). After restoring the config and running `ccs resume --profile …`, the session continues with the resume prompt.
- [ ] Holds survive `ccs daemon restart` (verified).
- [ ] The manual pages above are updated.

## Risks & mitigations
- **Pausing too late between polls:** statusline live reports (P07) feed the policy within seconds, plus 20 s fast polling near thresholds.
- **A false resume after a reset without fresh data:** confirmation requires fresh data; the time-based clear only after 10 min, with a warning.
- **Notification spam:** instance-keyed dedupe, hour buckets for errors, per-type toggles.
- **Model matching by substring is wrong for future names:** `name.lower() in model_id.lower()` is isolated in one function with tests. Revisit when new scoped buckets appear.
- **A lost ack leaves the state ambiguous:** `was_busy_at_pause=None` means no auto-prompt (safe default), and it is logged.
