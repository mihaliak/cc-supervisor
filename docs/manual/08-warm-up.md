# Warm-up

> Status: shipped (2026-09-24).

## Why
The 5-hour session window starts at your **first** usage. If your first prompt of the day is at 09:30, the window runs until 14:30. A **warm-up** sends one tiny request earlier (say at 06:00, or when you unlock the Mac), so the window starts earlier and resets earlier. More windows fit into your day, and the reset lands before you need it.

A warm-up costs a negligible amount of usage. It uses a small model and a one-word answer.

## What a warm-up runs
For the profile's config dir, CC Supervisor runs Claude Code once, non-interactively:
- **model**: `haiku` by default (per profile, `warmup.model`)
- **prompt**: `Reply with just: ok` by default (per profile, `warmup.prompt`)
- it runs in an empty scratch folder, with your hooks disabled, and **nothing is saved** to your conversation history

Afterward, usage is fetched again to confirm the window started. Claude's usage numbers can lag by a minute or more, so if the window isn't visible yet it re-checks for up to about 3 minutes, and you get a notification: "💼 Work: session window started, resets 11:02".
- If Claude Code exits with an error, takes longer than 2 minutes (it is stopped), or usage still shows no active window, the warm-up **failed** and you get a notification with the reason, e.g. `exit_1`, `timeout`, `window_not_started`, or `error` (an unexpected problem; the notification names it).
- Only one warm-up runs per profile at a time.

## Triggers
Each trigger can be switched on or off per profile. In the app (Settings → Profiles → *profile* → Warm-up):
- toggles for the whole warm-up and for app start, unlock/wake and auto-chain
- model (free text, with suggestions `haiku`, `sonnet`, `opus`, `fable`) and prompt
- scheduled times: a time picker plus weekday chips (Mon–Sun) per row, a trash button, and **Add time** (adds 06:00 Mon–Fri)
- active hours (two time pickers) and a cooldown stepper (1–120 minutes; other values with `ccs profile set`)
- read-only **Next warm-up** and **Last attempt** (with its result and reason)


| Trigger | Fires when | Default |
|---------|-----------|---------|
| **Schedule** | At listed times on chosen weekdays, e.g. `06:00` Mon–Fri | no times set |
| **App start** | The menu bar app launches (e.g. at login) | on |
| **Unlock / wake** | You unlock the screen or the Mac wakes from sleep | on |
| **Auto-chain** | A session window resets during your **active hours**, so the next one starts right away | on |

- **Active hours** default to `07:00–23:00` and limit auto-chain, and also missed schedules (below).
  - The start time is included and the end time is not: `07:00–23:00` covers 07:00 through 22:59.
  - A range can cross midnight, e.g. `22:00–06:00`. The same start and end (e.g. `00:00–00:00`) means the whole day.
- **Cooldown** defaults to 10 minutes: at most one warm-up attempt per profile in that time. It starts when an attempt begins; skipped warm-ups don't count.
- **Auto-chain timing:** about 15 seconds after the scheduled reset, usage is checked to confirm the window really reset. If the data still shows the old window, it checks again every 30 seconds for up to 10 minutes, then gives up for that window.

## Skip rules
Every trigger is checked against these rules, in order. A skipped warm-up is logged with its reason (`ccs events`); skips never notify.
1. `disabled`: warm-up, or that trigger, is turned off for the profile. A schedule counts as off when it has no times.
2. `needs_sign_in`: the profile needs sign-in.
3. `window_active`: **a session window is already active.** There's nothing to start, so most unlocks are skipped.
4. `session_busy`: a `ccs` session of this profile is working right now, so the window starts anyway.
5. `cooldown`: still within the cooldown since the last attempt.
6. `outside_active_hours`: auto-chain, or a missed-schedule catch-up, outside active hours.
7. `weekly_hold`: the profile is paused on its **weekly** limit.

A warm-up with `--force` ignores rules 3–7, but never 1–2. A trigger that fires while that profile's warm-up is still running is skipped as `in_progress`.

## Scheduled times
- Each scheduled time runs **once**, even if the supervisor restarts. After a restart, times missed within the last 10 minutes still run.
- **Daylight saving:** a time that doesn't exist on the spring-forward day (e.g. `02:30`) runs at the first valid minute after (`03:00`). A time that happens twice on the fall-back day runs only the first time.

## Missed schedules
If the Mac was asleep (or the clock jumped) at a scheduled time, **one** warm-up runs **when it wakes**, for the latest missed time, but only if:
- it's still the same day (a time missed yesterday is dropped)
- it's inside active hours
- no window is active (and the other skip rules pass)

Waking also counts as an **unlock / wake** trigger, evaluated right after the catch-up. When the catch-up already started a warm-up, the unlock one is skipped.

## Example day
Profile `work`: schedule `06:00` Mon–Fri, all other triggers on, active hours `07:00–23:00`.
```
06:00  schedule         → warm-up → window starts, resets ≈ 11:00
08:30  unlock           → skipped: window already active
11:00  window resets    → auto-chain → next window, resets ≈ 16:00
16:00  window resets    → auto-chain → next window, resets ≈ 21:00
21:00  window resets    → auto-chain → next window, resets ≈ 02:00
02:00  window resets    → no auto-chain (outside active hours)
```
Scheduled times fire even outside active hours: the 06:00 run above happens before 07:00. Active hours only restrict auto-chain and missed schedules.

## Run one manually
```sh
ccs warmup --profile work          # respects the skip rules
ccs warmup --profile work --force  # start now even if a window is active, etc.
ccs warmup --all                   # every profile
```
```
💼 work: started (window will be confirmed; see ccs events --follow)
🏠 personal: skipped (window_active, resets 20:00)
```
- The background supervisor must be running (`ccs daemon start`), because it runs the warm-up.
- The command returns right away. The result (window started, or failed) arrives as an event and a notification.

You can also use menu bar → **Warm up now ▸ profile**.

## Where to see what happened
- `ccs events` lists `warmup.started`, `warmup.skipped` (with the reason), `warmup.succeeded` (with the new reset time), and `warmup.failed`.
- `ccs status` shows each profile's next planned warm-up (`next_warmup_at`), which is also shown in the widgets and the menu bar.
- `~/.local/state/ccs/warmup/<profile>.json` keeps the last attempt, the last skip, the next planned run, and the last 20 attempts.

## Settings reference
| Key | Default |
|-----|---------|
| `warmup.enabled` | `true` |
| `warmup.model` | `"haiku"` |
| `warmup.prompt` | `"Reply with just: ok"` |
| `warmup.triggers.schedule` | `[]`, e.g. `[{"time":"06:00","weekdays":["mon","tue","wed","thu","fri"]}]` |
| `warmup.triggers.app_start` | `true` |
| `warmup.triggers.unlock_wake` | `true` |
| `warmup.triggers.auto_chain` | `true` |
| `warmup.active_hours` | `{"start":"07:00","end":"23:00"}` |
| `warmup.cooldown_minutes` | `10` |

Full details are in the [Configuration reference](10-configuration-reference.md).
