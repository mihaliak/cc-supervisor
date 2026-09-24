# Statusline

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

Each profile gets its own Claude Code statusline. It shows which account you're on, the folder, the model and effort, and your session usage with its reset time, plus any supervisor state.

## Format
```
{emoji} {name} ~ {folder} ~ {model} / {effort} ~ {session usage}
```
| Part | Source |
|------|--------|
| `{emoji} {name}` | profile emoji and name, for example `💼 Work` |
| `{folder}` | name of the current working folder |
| `{model}` | model display name, for example `Opus 5.5` |
| `/ {effort}` | reasoning effort (`low` … `max`). The whole `/ {effort}` part is left out for models without effort. |
| `~` | separator, shown in gray |

The percent and the bar are colored **green** (below 50%), **yellow** (50–79%), or **red** (80% and above).

## Session usage states
**Normal**
```
💼 Work ~ cc-supervisor ~ Opus 5.5 / xhigh ~ 45% ▓▓▓▓▓░░░░░ 20:00 (in 2h 13m)
```
**Warning** (at or above the warn threshold, default 80%)
```
💼 Work ~ cc-supervisor ~ Opus 5.5 / xhigh ~ ⚠ 84% ▓▓▓▓▓▓▓▓░░ 20:00 (in 42m) · limit approaching
```
**Paused** by the supervisor (see [Limits & supervisor](07-limits-and-supervisor.md))
```
💼 Work ~ cc-supervisor ~ Opus 5.5 / xhigh ~ ⏸ 90% ▓▓▓▓▓▓▓▓▓░ paused → resumes 20:00 (in 42m)
```
If the pause comes from the weekly or model-scoped limit, the resume time shown is that limit's reset.

**Paused manually** (`ccs pause`, or Pause in the menu bar; there's no end time)
```
💼 Work ~ cc-supervisor ~ Opus 5.5 / xhigh ~ ⏸ 45% ▓▓▓▓▓░░░░░ paused (manual)
```
**Overridden** (you continued manually while paused)
```
💼 Work ~ cc-supervisor ~ Opus 5.5 / xhigh ~ ⚠ 91% ▓▓▓▓▓▓▓▓▓░ 20:00 (in 42m) · override
```
**No data** (not fetched yet, or older than 10 minutes)
```
💼 Work ~ cc-supervisor ~ Opus 5.5 / xhigh ~ ?% ░░░░░░░░░░
```

## Extra segments
These appear only when they matter: at or above the warn threshold, or when they cause the current pause. They're added in this order:

| Segment | Shown when | Example |
|---------|-----------|---------|
| Weekly | weekly ≥ warn | ` ~ W ⚠ 86% Sat 08:00` |
| Model-scoped | e.g. Fable ≥ warn | ` ~ Fable ⚠ 82% Sat 08:00` |
| Extra usage | "Spill into credits" is active **and** one of: extra usage ≥ warn, an extra-usage pause is active, or session ≥ 100% | ` ~ € 3.20/10.00` |
| Supervisor offline | the background supervisor hasn't updated usage for more than 5 minutes | ` ~ ⚠ supervisor offline` |

Full example:
```
💼 Work ~ api ~ Opus 5.5 / high ~ 45% ▓▓▓▓▓░░░░░ 20:00 (in 2h 13m) ~ W ⚠ 86% Sat 08:00
```

## Bar and times
- The bar has 10 cells (`▓` filled, `░` empty). The filled count is the percent divided by 10, rounded.
- Reset time is 24h local time:
  - same day: `20:00`
  - within 6 days: `Sat 08:00`
  - later: `26 Sep 08:00`
- Countdown: `in 2h 13m`, `in 42m`, `in 3d 4h`, `in <1m`, `now`.

## Where the numbers come from
- Claude Code gives the statusline live session and weekly usage after every response. That is the freshest source, so it wins.
- Otherwise, the last value the supervisor fetched is used.
- The script also reports those live numbers back to the supervisor, which is how pauses react within seconds while you work.

## Turning it on
### Automatically with `ccs`
If the profile's statusline is enabled (the default), every `ccs --<profile>` session shows it. `ccs` passes it to Claude for that session only and regenerates the script if needed. Nothing is written to your `settings.json`.

### For plain `claude` too: apply
To also see it when you run `claude` directly with that config dir (for example `CLAUDE_CONFIG_DIR=~/.claude-work claude`), apply it:
- **App:** Settings → Profiles → *profile* → Statusline → **Apply**
- **Terminal:** `ccs statusline apply --profile work`

What `apply` does:
1. Writes the script to `<config dir>/ccs-statusline.py`, for example `~/.claude-work/ccs-statusline.py`. It's self-contained, with your profile's name, emoji, and colors baked in.
2. Backs up `settings.json` to `<config dir>/settings.json.ccs-backup-<YYYYmmddHHMMSS>`.
3. Sets `statusLine` in `<config dir>/settings.json` to run that script with CC Supervisor's Python. All your other settings stay untouched.

In plain `claude` sessions, the statusline shows usage but never shows ⏸: only `ccs` sessions are supervised.

### Undo: revert
- **App:** Settings → Profiles → *profile* → Statusline → **Revert**
- **Terminal:** `ccs statusline revert --profile work`

This restores the `statusLine` you had before `apply`.

### Other commands
```sh
ccs statusline preview --profile work    # print example lines for every state
ccs statusline generate --profile work   # rewrite the script only (settings.json untouched)
```
Regenerate after changing the profile's name, emoji, or colors. The app and `ccs` do this for you in normal use.

## Performance
The script uses only the Python standard library and starts without site packages. It is designed to finish in under 60 ms, so it never slows Claude Code down.
