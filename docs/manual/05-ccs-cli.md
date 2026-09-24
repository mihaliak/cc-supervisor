# `ccs` command reference

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

`ccs` does two jobs:
- **launcher**: `ccs --work` opens classic Claude Code under supervision
- **control tool**: status, profiles, sign-in, statusline, warm-ups, and the background supervisor

The sample outputs below are illustrative.

## Launching Claude Code
```
ccs [--<flag> | --profile <id>] [--force] [--no-supervise] [claude args…]
```
```sh
ccs --work                         # work profile, new session
ccs --work -c                      # continue last conversation
ccs --personal --model opus "fix the failing test"
ccs --profile work --resume        # same as --work
ccs                                # default profile (default_profile in config)
```
- You get the **normal interactive Claude Code**: the same TUI, keys, mouse, and resizing. `ccs` stays invisible in between and never prints over Claude.
- **The leading ccs options select the profile and modes:** `--<flag>` / `--profile <id>`, `--force`, and `--no-supervise`, in any order, at the front. Everything from the first other argument on goes to `claude` unchanged (e.g. `ccs --force -c` uses the default profile with `--force` and passes `-c`).
- `ccs` sets `CLAUDE_CONFIG_DIR` to the profile's config dir, plus `CCS_PROFILE`, `CCS_WRAPPER_ID`, and `CCS_STATE_DIR`.
- If the profile's statusline is enabled, `ccs` turns it on for this session, even if you never ran `ccs statusline apply`. It regenerates the script first if it is missing or outdated.
- The session is **supervised**: it can be paused and resumed ([Limits & supervisor](07-limits-and-supervisor.md)).
- `ccs` exits with Claude's exit code.

Options:
- `--force`: skip the "profile is paused" question (below).
- `--no-supervise`: run without registering with the supervisor. Useful for debugging. The statusline still works.

**Starting while a profile is paused:**
```
Profile work is paused until 20:00 (in 42m). Start anyway? [y/N]
```
If you answer `y`, or pass `--force`, the session starts as **overridden**: it runs freely for the rest of that window.

**When the supervisor isn't running:**
- Installed but stopped: `ccs` starts it automatically.
- Not installed: `ccs` prints one hint line (`ccs daemon install`) and starts Claude **unsupervised**. The statusline then shows `⚠ supervisor offline`.
- If the supervisor restarts mid-session, `ccs` reconnects on its own, and paused state is kept.

## Observe
### `ccs status [--profile <id>] [--json]`
Usage plus supervisor state plus daemon health.
```
Supervisor: running · polling every 60s
💼 Work      Session 90% 20:00 (in 42m) ⏸ paused
             Weekly 61% Sat 08:00 · Fable 12% Sat 08:00 · Extra usage off
             ccs sessions: 3 active, 2 paused · other sessions: 1 · next warm-up 20:00
🏠 Personal  Session  1% 20:00 (in 3h 52m)
             Weekly 50% Sat 08:00 · Fable 4% Sat 08:00
             ccs sessions: 0 · next warm-up Fri 06:00
```

### `ccs usage [--profile <id>] [--refresh] [--json]`
Only the usage numbers. `--refresh` fetches fresh data instead of using the last snapshot. When the supervisor is running, it refreshes through it; otherwise it asks Claude Code directly and just prints the result.
```
💼 Work   Session 45%  20:00 (in 2h 13m)
          Weekly  50%  Sat 08:00 (in 1d 13h)
          Fable    4%  Sat 08:00 (in 1d 13h)
          Extra usage off · €0.00 / €10.00
```

### `ccs sessions [--profile <id>] [--json]`
Supervised `ccs` sessions, plus other Claude Code sessions of each profile (background agents and plain `claude`), which are listed but not supervised.
```
PROFILE  ID        FOLDER          ACTIVITY  SUPERVISION
work     3f9c1a2e  cc-supervisor   idle      paused → 20:00
work     b71e04d9  api             busy      overridden
work     –         cosmos          –         other (background agent)
```

### `ccs events [--follow] [--json]`
The event history (warnings, pauses, resumes, warm-ups, errors). `--follow` keeps streaming.
```
2026-09-24 19:18  work  limit.pause   Work paused at 90%: 2 sessions. Resumes 20:00 (in 42m)
2026-09-24 20:00  work  limit.resume  Work resumed: 2 sessions continued
```

## Control
### `ccs pause (--profile <id> | --session <wrapper_id>) [--json]`
Pause now, exactly like an automatic pause. A busy session is interrupted.

### `ccs resume (--profile <id> | --session <wrapper_id>) [--json]`
Resume now. Sessions interrupted mid-work get the profile's resume prompt. Automatic pausing won't hit them again for the rest of the current window.

### `ccs warmup (--profile <id> | --all) [--trigger <t>] [--force] [--json]`
Start a session window now ([Warm-up](08-warm-up.md)).
- `--trigger` is one of `manual` (default), `app_start`, `unlock_wake`, `schedule`, or `auto_chain`.
- `--force` ignores "window already active", "session busy", "cooldown", "outside active hours", and "weekly hold". It still respects "warm-up disabled" and "sign-in required".

## Setup
### `ccs auth login [--terminal] | status | logout --profile <id> [--json]`
Claude Code's own sign-in for the profile's config dir ([Profiles & sign-in](02-profiles-and-sign-in.md)). `--terminal` is the variant the app uses when sign-in has to run in a terminal window.

### `ccs statusline generate | apply | revert | preview --profile <id> [--json]`
Details in [Statusline](06-statusline.md).

| Subcommand | Does |
|------------|------|
| `generate` | Writes or refreshes `<config dir>/ccs-statusline.py` |
| `apply` | Generates the script, backs up `settings.json`, and sets its `statusLine` to the script, so plain `claude` shows it too |
| `revert` | Restores the previous `statusLine` from the backup |
| `preview` | Prints sample lines for each state |

### `ccs profile …`
```sh
ccs profile list [--json]
ccs profile show <id> [--json]
ccs profile add --id <id> --flag <flag> --name <name> --emoji <emoji> --config-dir <dir> [--default]
ccs profile set <id> <dotted.key>=<value> [<dotted.key>=<value>…]
ccs profile remove <id> [--default <other>]
```
- `add --default` also makes the new profile the default one (used by plain `ccs`).
- `remove` deletes only the profile entry. Your Claude config dir and its login stay untouched. To remove the default profile while others exist, name the new default with `--default <other>`.
`set` values are parsed as JSON when valid, otherwise as strings:
```sh
ccs profile set work limits.weekly.pause=97 limits.model_scoped.warn_only=true
ccs profile set work supervisor.resume_prompt="Limits reset. Continue the task."
```

### `ccs config path | show | validate | defaults | set [--json]`
| Subcommand | Does |
|------------|------|
| `path` | Prints the config file location |
| `show` | Prints the effective config, with defaults filled in |
| `validate` | Checks the file and lists errors with their key paths |
| `defaults` | Prints every default value |
| `set` | Changes top-level settings, e.g. `ccs config set default_profile=work display.menu_bar=icon_only`. Profiles are changed with `ccs profile set`. |

### `ccs daemon install | uninstall | start | stop | restart | status | run | logs [--json]`
The background supervisor (a LaunchAgent).
- `install` registers it and captures your current `PATH`, so it can find `claude`.
- `start`, `stop`, and `restart` are for everyday use.
- `logs` shows recent daemon log lines.
- `run` is what launchd executes. You don't normally run it yourself.

### `ccs doctor [--json]`
Checks everything and suggests fixes. See [Troubleshooting](11-troubleshooting.md).

### `ccs --version`

## JSON output
Every command above accepts `--json` and then prints only JSON on stdout (diagnostics go to stderr). This is what the menu bar app uses, and it's safe for scripts.

## Exit codes
| Code | Meaning |
|------|---------|
| 0 | OK |
| 1 | Error (details on stderr, or in the JSON `error` field) |
| 2 | Invalid usage (unknown option, bad arguments) |
| any | The launcher returns Claude's own exit code |
