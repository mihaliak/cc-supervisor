# Limits & supervisor

> Status: shipped (2026-09-24).

The supervisor runs in the background (see [Installation](01-installation.md)) and watches every profile's limits. It:
- **warns** you when a limit gets close
- **pauses** your `ccs` sessions before the limit is used up, leaving headroom
- **resumes** them automatically when the window resets

## The background supervisor
One supervisor process (`ccs daemon`, a LaunchAgent) runs per Mac and handles every profile:
- asks Claude Code for each profile's usage on a schedule (below). It uses the profile's own login, costs no tokens, and runs no hooks.
- merges in the fresher numbers your running sessions' statuslines report after every Claude response
- keeps the files the widgets, menu bar and statusline read (`~/.local/state/ccs/`) up to date, and rewrites them after every check, even a failed one
- counts other Claude Code sessions of each profile (background agents, plain `claude`) about once a minute, and every 15 s while `ccs` sessions run
- notices when a `ccs` session's terminal closed or crashed, and forgets it within ~10 s
- picks up config changes within ~2 s. An invalid config is ignored (the last good one stays active) and reported once.

Manage it with `ccs daemon …` ([ccs CLI](05-ccs-cli.md)). Its log is `~/.local/state/ccs/logs/daemon.log`.

## Why pause before 100%?
Claude Code already stops at 100% and continues by itself after the reset. The supervisor stops **earlier**, so some capacity stays free for quick manual work, and so running agents don't drain the window to zero.

## Limits and defaults
All thresholds are per profile and editable (Settings → Profiles → *profile* → Limits, a Warn/Pause grid from 1 to 100 %, or `ccs profile set`). The **Fable warn-only** and **Spill into credits** toggles are in the same section. A warn value that isn't below its pause value is saved but marked invalid, and the supervisor keeps the previous valid limits until you fix it.

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
                         notification: "💼 Work: session at 80%" — "Resets 20:00 (in 5h 50m)"
17:35  Session at 90%  → busy ccs sessions are interrupted (Esc)
                         statusline: ⏸ 90% … paused → resumes 20:00 (in 2h 25m)
                         notification: "💼 Work paused at 90%" — "2 sessions paused. Resumes 20:00 (in 2h 25m)"
20:00  Window resets   → supervisor checks fresh usage 15 s after the reset, then every 30 s
20:00  Confirmed       → interrupted sessions get the resume prompt and keep working
                         notification: "💼 Work resumed" — "2 sessions continued"
```
- The reset counts as confirmed when fresh usage shows a new window (a later reset time) or the percent has dropped below the warn threshold.
- The pause is decided and saved first (`~/.local/state/ccs/supervisor/<profile>.json`), then the sessions are told. Pauses survive a restart of the supervisor; reconnecting `ccs` sessions stay paused and are resumed once, as usual.
- A pause that couldn't reach a session (for example, the supervisor had just restarted and the session was still reconnecting) is sent again when the session reconnects. A session that already got it is never interrupted twice.

## What "pause" does in a `ccs` session
- **Busy session** (Claude is working, running a tool, or waiting on a permission prompt): the supervisor sends **Esc**, the same as pressing it yourself. Claude stops the current turn. The TUI stays open.
  - It checks again 2 s later and sends one more Esc if Claude is still working, unless you submitted something in the meantime (that's an override, see below).
  - **Subagents and workflows are stopped too.** Esc alone never stops them, and a session counts as busy while any of them runs, even when the main turn is idle. If the session is still busy after the first Esc, the supervisor:
    - presses **ctrl+x ctrl+k** twice, which stops all background agents
    - stops each still-running workflow from the prompt footer (↓, `x`, Backspace)
    - sends a last Esc for the short turn Claude starts when it's told the agents were stopped

    This takes about 8–20 s. The footer step is skipped when you might have a draft in the prompt (you typed after your last submit), and it stops as soon as you type.
  - If it can't tell whether Claude is busy, it sends the Esc anyway. Such a session gets no resume prompt later, because it may have been idle.
- **Idle session**: nothing is typed. It's only marked paused.
- Only Esc, the stop keys above, and the resume prompt are ever typed for you. Never slash commands.
- The statusline shows **⏸ … paused → resumes HH:MM**. The widget and menu bar show **⏸ Paused**.
- **At reset**, sessions that were interrupted mid-work get the profile's **resume prompt** typed in and submitted:
  > The usage limit window has reset. Continue exactly where you left off.

  Change it in Settings → Profiles → *profile* → Limits → Supervisor, or with `ccs profile set <id> supervisor.resume_prompt="…"`.

  If the pause stopped subagents or workflows, a note is appended so Claude starts them again. Otherwise it would take "stopped by the user" at face value:
  > Background agents or workflows stopped at the pause were stopped by the usage pause, not by the user: start again any that had not finished.
- Sessions that were idle when paused get no prompt. They are just un-paused.
- If you typed anything in the session while it was paused, even without submitting, no prompt is typed, so it can't mix with your draft. Switching windows, clicking, or scrolling doesn't count as typing. This holds even if the session only learned of the pause after reconnecting: typing since the pause began counts.
- The resume prompt is typed only once Claude is idle. If Claude is still working 30 s after the reset, the prompt is skipped.
- The supervisor never types while you're typing. It waits until you've stopped typing for 1.5 s (at most 30 s). Mouse and focus events don't count as typing, so they don't delay it.

## Typing while paused (manual override)
You can always type in a paused session. If you **submit** a prompt (Enter; pasted text and newlines typed with Option/Shift+Enter or Ctrl-J don't count), that session becomes **overridden**:
- it runs freely until the window resets and won't be interrupted again in this window
- it won't get the automatic resume prompt
- the statusline shows `⚠ 91% … · override`, and a `limit.override` event is logged (no notification)
- if the supervisor was down when you submitted, the override is reported as soon as it's back
- the override covers only the limits that were paused when you typed. If a *different* limit hits later (for example the weekly one), the session is paused again for that one.
- after the window resets, the session is simply back to normal (no prompt is typed)

## Starting a new session while paused
```
$ ccs --work
Profile work is paused until 20:00 (in 42m). Start anyway? [y/N]
```
- This appears for any active pause, including a manual one. A manual pause has no end time, so it reads `Profile work is paused (manual). Start anyway? [y/N]`.
- `N` (the default) exits.
- `y`, or `ccs --work --force`, starts the session as **overridden**. Ctrl-C at the question means no.
- This also covers an active model-scoped pause (e.g. Fable): the session's model isn't known at start, so once it is and it matches, the session shows as overridden instead of being paused. A session on another model just runs normally.

## Weekly limit
Same idea as the session limit, with a later pause point (95% by default), because a weekly pause can last days. Warnings start at 80%, and the statusline shows `~ W ⚠ 86% Sat 08:00` once the weekly usage reaches the warn threshold. Warm-ups are skipped while a weekly pause is active.

## Model-scoped limits (e.g. Fable)
Some accounts have a separate weekly limit for a specific model. Per profile, choose:
- **Default (Fable warn-only off):** at 95%, only `ccs` sessions **currently using that model** are paused. Sessions on other models keep going. They resume when that model's limit resets.
  - "Using that model" means the session's model id contains the limit's name (e.g. `claude-fable-5-1` for Fable). The model comes from the session's statusline, so a session that hasn't answered yet (model unknown) isn't paused by it.
  - A session that switches to that model while its limit is paused is paused within a second or two.
- **Fable warn-only on:** never pauses. You get warnings at 80% and at 95%, and other models remain usable.

The statusline shows `~ Fable ⚠ 82% Sat 08:00` at the warn threshold and above.

## Extra usage (paid credits)
If extra usage is enabled on your account, Claude continues on credits after the plan limits run out. Per profile:
- **Spill into credits off (default):** pauses happen as normal. Credits are only shown, with a warning at 80% of your monthly cap.
- **Spill into credits on:** session and weekly **pauses are skipped** (their warnings still come), because work continues on credits. Instead, the supervisor pauses at **90% of the monthly credit cap**. The statusline shows spend as `~ € 3.20/10.00`, and a credit-cap pause as `⏸ … paused (credits)`.
  - If credits are actually disabled on the account (for example, out of credits), spill has no effect and the normal pauses apply.
  - Turning spill on while a session or weekly pause is active resumes those sessions right away. Turning it off again pauses as normal, even in the same window.
  - The credit-cap pause lifts when the credit percent drops below 90% (a new month or a higher cap), when credits are switched off, or when you turn spill off.
- The credit warning and pause each fire once per cap. They re-arm only after the credit percent drops below the warn threshold (for example after the monthly reset) or the cap changes, never just because a calendar month started.

## What is NOT paused
- **Background sessions** (`claude --bg`, the agent view). Subagents and workflows *inside* a `ccs` session are stopped with it (see above).
- **Plain `claude` sessions** not started with `ccs`.

They're counted and shown as "other sessions" in the menu bar, the large widget, and `ccs sessions`, but never interrupted.

## Each window pauses once
- A limit warns once and pauses once per window. After a resume or an override, the same window won't pause you again.
- A fresh window (new reset time) starts clean.
- Reset times within a minute of a window the supervisor already knows count as that window, so the small jitter in what Claude Code reports never looks like a new window (no second warning, no second pause, an override still holds).

## Where the numbers come from
- **Claude Code itself.** The supervisor asks each profile's Claude Code for its usage, the same numbers `/usage` shows, using that profile's own sign-in. It takes about a second, uses no tokens, and never runs your hooks. CC Supervisor stores no passwords or tokens.
- **Your running sessions.** Every statusline refresh reports the session and weekly percent Claude Code just received, so the supervisor reacts within seconds while you work. Model-scoped (Fable) and extra-usage numbers come only from the regular checks.
- For the same window, the higher number wins, because usage only rises within a window. A statusline redraw showing an older number can't lower a fresher check. For a new window, the newest number wins.
- Reset times are compared to the minute, so tiny timing differences never look like a new window.

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
- right after a reset, a warm-up, or a change to a profile's config dir (a newly added profile is checked immediately)
- whenever you click Refresh or run `ccs usage --refresh`
- live, from the statusline, while you work

Each interval is at most 240 s (`polling.*` in the [config](10-configuration-reference.md)), so the supervisor's files never look older than the 5-minute offline limit.

Only one check per profile runs at a time. Refresh requests that arrive during a check are combined into a single follow-up check.

## Manual control
```sh
ccs pause --profile work          # pause all work sessions now
ccs resume --profile work         # resume them now
ccs pause --session 3f9c1a2e      # one session (short ids from `ccs sessions`)
ccs resume --session 3f9c1a2e
```
- A manual pause lasts until you resume it manually. It doesn't end at a reset. A single-session pause (`--session`) also ends when that session exits, and its hint names `ccs resume --session <id>`. Its statusline reads `⏸ … paused (manual)` and the notification "💼 Work paused manually".
- A manual resume of the profile clears every pause (manual and automatic). It works like an automatic resume: interrupted sessions get the resume prompt, and the current window won't pause them again.
- Resuming a single session while the profile is still paused lets that one session continue as **overridden**.
- These need the supervisor running (`ccs daemon start`).
- The menu bar has the same actions under **Pause / Resume profile**.

## Turning supervision off
Settings → Profiles → *profile* → Limits → **Pause `ccs` sessions at the limits** off (`supervisor.enabled`).
- Usage display, warnings (statusline and notifications), and warm-ups keep working.
- Nothing is paused. Pauses that are active when you switch it off are lifted right away (interrupted sessions get the resume prompt), and `ccs pause` is refused for that profile.
