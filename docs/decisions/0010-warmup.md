# ADR-0010: Warm-up sessions

- Status: accepted
- Date: 2026-09-24
- Source: user decisions (all four triggers, per-profile model with Haiku default) + planner defaults (skip rules, cooldown)

## Context
The 5-hour session window starts at the first usage. Starting it early (for example 06:00, or at unlock) moves resets earlier in the day, so more total windows fit into working hours.

## Decision
- **Triggers** (per profile, each toggleable):
  1. `schedule`: a list of `{time: "HH:MM", weekdays: [...]}`.
  2. `app_start`: the menu bar app launched (for example at login).
  3. `unlock_wake`: screen unlocked or woke from sleep. The app observes `com.apple.screenIsUnlocked` and `NSWorkspace.didWakeNotification`.
  4. `auto_chain`: the session window reset during `active_hours`, so start the next one immediately.
- **Who runs it:** the app forwards trigger 2 and 3 as `ccs warmup --all --trigger app_start|unlock_wake`. The daemon owns triggers 1 and 4, the skip rules, and execution.
- **Skip rules** (in order; each skip emits `warmup.skipped` with a reason):
  1. The profile's warm-up is disabled, or that trigger is disabled.
  2. Auth is missing (`needs_sign_in`).
  3. The session window is already active: `windows.session.resets_at` is in the future. The exact inactive-window shape is verified in P00-S5.
  4. A supervised session of the profile is busy, so the window will start anyway.
  5. Within `cooldown_minutes` of the last warm-up attempt for this profile.
  6. `auto_chain` outside `active_hours`.
  7. The profile has an active weekly hold (no point starting a session window).
- **Execution:**
  - Command: `claude -p "<prompt>" --model <model> --no-session-persistence --settings '{"disableAllHooks":true}'`
  - Environment: `CLAUDE_CONFIG_DIR`; working directory `warmup/cwd/`; timeout 120 s.
  - Then a forced usage poll confirms the window started.
  - Events: `warmup.succeeded` (with the new `resets_at`) or `warmup.failed`.
- **Missed schedules:** if the Mac slept through a scheduled time, run it on the next wake, provided it is still the same day, inside `active_hours`, and the window is inactive. The daemon also detects wall-clock jumps larger than 2× its tick.
- **Defaults:** model `haiku`, prompt `Reply with just: ok`, schedule empty, the other triggers on, active hours `07:00–23:00`, cooldown 10 min.

## Rules for implementers
- Warm-ups never run in parallel for the same profile. Hold a per-profile asyncio lock.
- `ccs warmup --force` bypasses rules 3–7 but not rules 1–2.

## Verification (P00-S5, 2026-09-24)
- The exact warm-up command (run with `stdin=DEVNULL`) exits 0 in ~3.4 s and prints `ok`.
- No persisted transcript or session files, no `~/.claude.json` bookkeeping, and a measured usage delta of 0 %. This was on a profile whose window was already active.
- Not yet observed, deliberately: that a warm-up on an **inactive** window sets `resets_at ≈ now + 5h`, because running it would start a real window. It gets confirmed at the user's first real warm-up (`warmup.succeeded` carries the new `resets_at`).
