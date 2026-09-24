# Warm-up

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

## Why
The 5-hour session window starts at your **first** usage. If your first prompt of the day is at 09:30, the window runs until 14:30. A **warm-up** sends one tiny request earlier (say at 06:00, or when you unlock the Mac), so the window starts earlier and resets earlier. More windows fit into your day, and the reset lands before you need it.

A warm-up costs a negligible amount of usage. It uses a small model and a one-word answer.

## What a warm-up runs
For the profile's config dir, CC Supervisor runs Claude Code once, non-interactively:
- **model**: `haiku` by default (per profile, `warmup.model`)
- **prompt**: `Reply with just: ok` by default (per profile, `warmup.prompt`)
- it runs in an empty scratch folder, with your hooks disabled, and **nothing is saved** to your conversation history

Afterward, usage is fetched again to confirm the window started, and you get a notification: "💼 Work: session window started, resets 11:02".

## Triggers
Each trigger can be switched on or off per profile (Settings → Profiles → Warm-up):

| Trigger | Fires when | Default |
|---------|-----------|---------|
| **Schedule** | At listed times on chosen weekdays, e.g. `06:00` Mon–Fri | no times set |
| **App start** | The menu bar app launches (e.g. at login) | on |
| **Unlock / wake** | You unlock the screen or the Mac wakes from sleep | on |
| **Auto-chain** | A session window resets during your **active hours**, so the next one starts right away | on |

- **Active hours** default to `07:00–23:00` and limit auto-chain, and also missed schedules (below).
- **Cooldown** defaults to 10 minutes: at most one warm-up attempt per profile in that time.

## Skip rules
Every trigger is checked against these rules, in order. A skipped warm-up is logged with its reason (`ccs events`).
1. Warm-up, or that trigger, is turned off for the profile.
2. The profile needs sign-in.
3. **A session window is already active.** There's nothing to start, so most unlocks are skipped.
4. A `ccs` session of this profile is working right now, so the window starts anyway.
5. Still within the cooldown since the last attempt.
6. Auto-chain outside active hours.
7. The profile is paused on its **weekly** limit.

A **manual** warm-up with `--force` ignores rules 3–7, but never 1–2.

## Missed schedules
If the Mac was asleep at a scheduled time, the warm-up runs **when it wakes**, but only if:
- it's still the same day
- it's inside active hours
- no window is active

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
You can also use menu bar → **Warm up now ▸ profile**.

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
