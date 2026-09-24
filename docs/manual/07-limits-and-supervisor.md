# Limits & supervisor

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

The supervisor runs in the background (see [Installation](01-installation.md)) and watches every profile's limits. It:
- **warns** you when a limit gets close
- **pauses** your `ccs` sessions before the limit is used up, leaving headroom
- **resumes** them automatically when the window resets

## Why pause before 100%?
Claude Code already stops at 100% and continues by itself after the reset. The supervisor stops **earlier**, so some capacity stays free for quick manual work, and so running agents don't drain the window to zero.

## Limits and defaults
All thresholds are per profile and editable (Settings → Profiles → Limits, or `ccs profile set`).

| Limit | Warn | Pause | What gets paused | Resumes when |
|-------|------|-------|------------------|--------------|
| **Session** (5-hour window) | 80% | 90% | all `ccs` sessions of the profile | the window resets |
| **Weekly** (all models) | 80% | 95% | all `ccs` sessions of the profile | the weekly limit resets |
| **Model-scoped**, e.g. Fable (default) | 80% | 95% | only `ccs` sessions currently using that model | that limit resets |
| Model-scoped with **Fable warn-only** on | 80% | – (a second warning at 95%) | nothing; other models stay usable | – |
| **Extra usage** (default) | 80% of monthly cap | – | nothing (shown and warned only) | – |
| Extra usage with **Spill into credits** on | 80% of cap | 90% of cap | all `ccs` sessions of the profile | the credit usage drops below 90% (monthly reset or a higher cap) |

A limit counts as reached when the percent is **at or above** the threshold.

## A typical session limit, step by step
```
14:10  Session at 80%  → statusline: ⚠ 80% … · limit approaching
                         notification: "💼 Work: session at 80%. Resets 20:00 (in 5h 50m)"
17:35  Session at 90%  → busy ccs sessions are interrupted (Esc)
                         statusline: ⏸ 90% … paused → resumes 20:00 (in 2h 25m)
                         notification: "💼 Work paused at 90%: 2 sessions. Resumes 20:00 (in 2h 25m)"
20:00  Window resets   → supervisor checks fresh usage (about 15 s after the reset)
20:00  Confirmed       → interrupted sessions get the resume prompt and keep working
                         notification: "💼 Work resumed: 2 sessions continued"
```

## What "pause" does in a `ccs` session
- **Busy session** (Claude is working): the supervisor sends **Esc**, the same as pressing it yourself. Claude stops the current turn. The TUI stays open.
- **Idle session**: nothing is typed. It's only marked paused.
- The statusline shows **⏸ … paused → resumes HH:MM**. The widget and menu bar show **⏸ Paused**.
- **At reset**, sessions that were interrupted mid-work get the profile's **resume prompt** typed in and submitted:
  > The usage limit window has reset. Continue exactly where you left off.

  Change it in Settings → Profiles → Supervisor, or with `ccs profile set <id> supervisor.resume_prompt="…"`.
- Sessions that were idle when paused get no prompt. They are just un-paused.
- The supervisor never types while you're typing. It waits until you've paused typing for a moment.

## Typing while paused (manual override)
You can always type in a paused session. If you **submit** a prompt (Enter), that session becomes **overridden**:
- it runs freely until the window resets and won't be interrupted again in this window
- it won't get the automatic resume prompt
- the statusline shows `⚠ 91% … · override`, and a `limit.override` event is logged

## Starting a new session while paused
```
$ ccs --work
Profile work is paused until 20:00 (in 42m). Start anyway? [y/N]
```
- This appears for any active pause, including a manual one. A manual pause has no end time, so it reads `Profile work is paused (manual). Start anyway? [y/N]`.
- `N` (the default) exits.
- `y`, or `ccs --work --force`, starts the session as **overridden**.

## Weekly limit
Same idea as the session limit, with a later pause point (95% by default), because a weekly pause can last days. Warnings start at 80%, and the statusline shows `~ W ⚠ 86% Sat 08:00` once the weekly usage reaches the warn threshold. Warm-ups are skipped while a weekly pause is active.

## Model-scoped limits (e.g. Fable)
Some accounts have a separate weekly limit for a specific model. Per profile, choose:
- **Default (Fable warn-only off):** at 95%, only `ccs` sessions **currently using that model** are paused. Sessions on other models keep going. They resume when that model's limit resets.
- **Fable warn-only on:** never pauses. You get warnings at 80% and at 95%, and other models remain usable.

The statusline shows `~ Fable ⚠ 82% Sat 08:00` at the warn threshold and above.

## Extra usage (paid credits)
If extra usage is enabled on your account, Claude continues on credits after the plan limits run out. Per profile:
- **Spill into credits off (default):** pauses happen as normal. Credits are only shown, with a warning at 80% of your monthly cap.
- **Spill into credits on:** session and weekly **pauses are skipped** (their warnings still come), because work continues on credits. Instead, the supervisor pauses at **90% of the monthly credit cap**. The statusline shows spend as `~ € 3.20/10.00`.
  - If credits are actually disabled on the account (for example, out of credits), spill has no effect and the normal pauses apply.

## What is NOT paused
- **Background agents** (`claude --bg`, the agent view).
- **Plain `claude` sessions** not started with `ccs`.

They're counted and shown as "other sessions" in the menu bar, the large widget, and `ccs sessions`, but never interrupted.

## Each window pauses once
- A limit warns once and pauses once per window. After a resume or an override, the same window won't pause you again.
- A fresh window (new reset time) starts clean.

## Where the numbers come from
- **Claude Code itself.** The supervisor asks each profile's Claude Code for its usage, the same numbers `/usage` shows, using that profile's own sign-in. It takes about a second, uses no tokens, and never runs your hooks. CC Supervisor stores no passwords or tokens.
- **Your running sessions.** Every statusline refresh reports the session and weekly percent Claude Code just received, so the supervisor reacts within seconds while you work. Model-scoped (Fable) and extra-usage numbers come only from the regular checks.
- The newest number wins. Reset times are compared to the minute, so tiny timing differences never look like a new window.

## When data is missing or stale
If fresh usage hasn't arrived for more than 10 minutes, or a profile needs sign-in:
- **no new pauses** start, because the supervisor never pauses on guesses
- existing pauses stay until the reset is confirmed. If the supervisor can't confirm the reset for 10 minutes after the scheduled time, it resumes anyway.
- the statusline shows `?%`

## How often usage is checked
| Situation | Interval |
|-----------|----------|
| normal | every 60 s |
| a limit ≥ 70% and a `ccs` session is working | every 20 s |
| no `ccs` sessions for the profile | every 120 s |

On top of that, usage is checked:
- right after a reset, a warm-up, or a config change
- whenever you click Refresh
- live, from the statusline, while you work

## Manual control
```sh
ccs pause --profile work          # pause all work sessions now
ccs resume --profile work         # resume them now
ccs pause --session <wrapper_id>  # one session (IDs from `ccs sessions`)
```
- A manual pause lasts until you resume it manually. It doesn't end at a reset.
- A manual resume clears every pause for that target. It works like an automatic resume: interrupted sessions get the resume prompt, and the current window won't pause them again.
- The menu bar has the same actions under **Pause / Resume profile**.

## Turning supervision off
Settings → Profiles → Supervisor → off (`supervisor.enabled`).
- Usage display, warnings (statusline and notifications), and warm-ups keep working.
- Nothing is paused.
