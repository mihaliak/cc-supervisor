# `ccs` command reference

> Status: shipped (2026-09-24).

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
- `ccs` sets `CLAUDE_CONFIG_DIR` to the profile's config dir, plus `CCS_PROFILE`, `CCS_WRAPPER_ID`, and `CCS_STATE_DIR`. Everything else in your environment passes through unchanged.
  - For the default dir `~/.claude`, `CLAUDE_CONFIG_DIR` is left **unset**, exactly like plain `claude`. Setting it explicitly would make Claude Code switch its global state from `~/.claude.json` to `~/.claude/.claude.json`, with different trust, MCP servers, and onboarding.
- If the profile's statusline is enabled, `ccs` turns it on for this session, even if you never ran `ccs statusline apply`. It regenerates the script first if it is missing or outdated. If you pass your own `--settings`, `ccs` doesn't inject it and prints one line saying so (use `ccs statusline apply` instead).
- The session is **supervised**: it can be paused and resumed ([Limits & supervisor](07-limits-and-supervisor.md)).
- **Ctrl-Z** suspends the whole thing like any job (`fg` brings Claude back and it repaints).
- **Print mode and pipes:** with `-p`/`--print`, or when stdin or stdout isn't a terminal, `ccs` simply runs `claude` with the profile's config dir. There is no supervision, so scripts and pipes behave exactly like `claude`.
- **Typos:** a flag that is close to a profile flag but isn't a Claude option is an error, e.g. `ccs --wrok` → `unknown profile '--wrok'. Did you mean --work?`. Other unknown flags go to `claude`.
- Two profile flags (`ccs --work --personal`) is an error.
- **Exit code:** Claude's exit code; `128 + signal` if Claude was killed by a signal; `127` if `claude` can't be found; `2` for launcher usage errors.

Options:
- `--force`: skip the "profile is paused" question (below).
- `--no-supervise`: run without registering with the supervisor. Useful for debugging. The statusline still works.

**Starting while a profile is paused:**
```
Profile work is paused until 20:00 (in 42m). Start anyway? [y/N]
```
If you answer `y`, or pass `--force`, the session starts as **overridden**: it runs freely for the rest of that window.

If the profile is paused only by a manual `ccs pause`, the question reads `Profile work is paused (manual). Start anyway? [y/N]`.

**When the supervisor isn't running:**
- Installed but stopped: `ccs` starts it automatically (like `ccs daemon start`).
- Installed but not answering after that: `ccs` prints `ccs: supervisor not responding — running unsupervised`, starts Claude, and keeps trying in the background. The session registers as soon as the supervisor answers.
- Not installed: `ccs` prints `ccs: supervisor not installed — running unsupervised (ccs daemon install)` and starts Claude **unsupervised**. The statusline then shows `⚠ supervisor offline`.
- If the supervisor restarts mid-session, `ccs` reconnects on its own (backing off from 0.5 s to 10 s), and paused state is kept.

## Observe
### `ccs status [--profile <id>] [--json]`
Usage plus supervisor state plus daemon health. The data comes from the running supervisor. If it isn't running, `ccs status` shows the last saved state and says so in the first line.
```
daemon: running (pid 4242, up 2h 13m)
💼 Work   Session   90%  20:00 (in 42m)
          Weekly    61%  Sat 08:00 (in 1d 13h)
          Fable     12%  Sat 08:00 (in 1d 13h)
          Extra usage off · €0.00 / €10.00
          sessions: 3 supervised (2 paused) · other: 1 interactive, 0 background
🏠 Personal  Session    1%  20:00 (in 3h 52m)
             Weekly    50%  Sat 08:00 (in 1d 13h)
             sessions: 0 supervised · other: 0 interactive, 2 background
```
- **"supervised"** counts `ccs` sessions. **"other"** counts background agents and plain `claude` sessions of that profile, which the supervisor only counts and never touches.
- `--json` prints `{"ok": true, "daemon": {…}, "profiles": [{"id", "usage", "supervisor", "sessions", "other_sessions", "next_warmup_at"}]}`.
  - `daemon` carries `version`, `pid`, `started_at`, `uptime_s` and `responsive: true`, or only `{"responsive": false}` when the supervisor isn't running.
  - `usage` is the `usage/<profile>.json` snapshot, and `sessions` are the supervised session records.

### `ccs usage [--profile <id>] [--refresh] [--json]`
Only the usage numbers, for every profile or just `--profile <id>`.
- **Without `--refresh`:** shows the supervisor's last snapshot, plus any newer numbers your running sessions' statuslines reported.
- **With `--refresh`:** fetches fresh data. When the supervisor is running, it asks it to poll now and waits up to 20 s. Otherwise it asks Claude Code directly (about 1–2 s per profile, all profiles in parallel) and **just prints** the result; it doesn't update the supervisor's files.
```
💼 Work       Session  45%  20:00 (in 2h 13m)
              Weekly   50%  Sat 08:00 (in 1d 13h)
              Fable     4%  Sat 08:00 (in 1d 13h)
              Extra usage off · €0.00 / €10.00
🏠 Personal   Session  26%  22:00 (in 1h 40m)
              …
```
A last line explains any problem:

| Line | Meaning |
|------|---------|
| `no data yet · run: ccs usage --refresh` | The supervisor hasn't polled this profile yet |
| `⚠ stale · updated 14m ago` | No fresh data for more than 10 minutes |
| `⚠ sign in required · run: ccs auth login --profile work` | The profile's Claude Code login is missing or expired |
| `no plan limits (API key or no Claude subscription)` | The account has no plan limits to track |
| `⚠ usage unavailable: …` | Claude Code couldn't report usage (see [Troubleshooting](11-troubleshooting.md)) |

`--json` prints `{"ok": true, "profiles": [ … ]}`. Each entry is the profile's usage snapshot (the `usage/<profile>.json` format, `schema/usage-snapshot.schema.json`) plus `"source"`: `file`, `daemon`, or `direct_probe`. A profile without data is `{"profile_id": "work", "status": "no_data", "hint": "…"}`.

### `ccs sessions [--profile <id>] [--json]`
Supervised `ccs` sessions (one line each), then how many other Claude Code sessions each profile has (background agents and plain `claude`), which are counted but never supervised.
```
3f9c1a2e  💼 work  ~/Code/cc-supervisor  claude-opus-5-5  idle  ⏸ paused → resumes 20:00 (in 42m)
b71e04d9  💼 work  ~/Code/api  claude-fable-5-1  busy  override
c0ffee12  🏠 personal  ~/Code/x  -  busy  ⏸ paused (manual)
work: other sessions 1 interactive, 2 background
```
- Columns: short session id (use it with `ccs pause|resume --session`), profile, folder, model, activity (`busy`, `idle`, `shell`, `waiting`, `unknown`), supervision (`running`, `⏸ paused …`, `override`).
- Without a running supervisor it shows the last saved state (`daemon: not running`).
- `--json` prints `{"ok": true, "daemon_responsive": bool, "sessions": [{"wrapper_id", "profile_id", "cwd", "model_id", "activity", "session_id", "started_at", "state", "holds", "resume_at"}], "other_sessions": {"<profile>": {"interactive", "background"} | null}}`.

### `ccs events [--follow] [--json]`
The event history: warnings, pauses, resumes, warm-ups, sign-in problems, sessions starting and ending, and the supervisor starting and stopping. Without `--follow` it prints the last 50 events.
```
19:18:04  limit.pause  work  💼 Work paused at 90% — 2 sessions paused. Resumes 20:00 (in 42m)
20:00:16  limit.resume  work  💼 Work resumed — 2 sessions continued
20:03:41  session.ended  work  💼 Work: session.ended
```
- `--follow` keeps streaming new events live from the supervisor, or by watching the event log file when the supervisor isn't running. Stop it with Ctrl-C.
- `--json` prints `{"ok": true, "events": [...]}`. With `--follow`, it prints one JSON event per line instead. The event format is `schema/event.schema.json`.

## Control
### `ccs pause (--profile <id> | --session <wrapper_id>) [--json]`
Pause now, exactly like an automatic pause: a busy session is interrupted (Esc), an idle one is only marked paused.
- `--profile` pauses every `ccs` session of the profile, and new `ccs` sessions ask before starting. `--session` pauses one session; the id can be the short id from `ccs sessions` (any unique prefix).
- A manual pause has no end time: it lasts until `ccs resume`. Running it twice is harmless (`already paused manually`).
- Needs the supervisor (`supervisor not running` otherwise, exit 1). Refused when the profile's supervision is off.
- `--json` prints `{"ok", "profile_id", "wrapper_id", "created", "hold": {"id": "manual", "scope", …}, "sessions_paused"}`.

### `ccs resume (--profile <id> | --session <wrapper_id>) [--json]`
Resume now. Sessions interrupted mid-work get the profile's resume prompt.
- `--profile` clears every pause of the profile (manual and automatic). The limits that were paused won't pause the profile again in their current window.
- `--session` resumes one session and lifts a manual pause on just that session. If the profile is still paused, that session continues as **overridden** (like typing into it) and isn't interrupted again for the current pause.
- `--json` prints `{"ok", "profile_id", "wrapper_id", "cleared": [holds], "sessions_resumed"}`.

### `ccs warmup (--profile <id> | --all) [--trigger <t>] [--force] [--json]`
Start a session window now ([Warm-up](08-warm-up.md)).
- `--trigger` is one of `manual` (default), `app_start`, `unlock_wake`, `schedule`, or `auto_chain`. The trigger decides which on/off switch and rules apply; `manual` has no switch.
- `--force` ignores "window already active", "session busy", "cooldown", "outside active hours", and "weekly hold". It still respects "warm-up disabled" and "sign-in required".
- It needs the background supervisor. When the daemon isn't running it exits 1 with `start it with: ccs daemon start`.
- It prints one decision per profile and returns immediately; started warm-ups finish in the background (`ccs events --follow`):
  ```
  💼 work: started (window will be confirmed; see ccs events --follow)
  🏠 personal: skipped (window_active, resets 20:00)
  ```
- `--json` prints `{"ok": true, "trigger": "manual", "results": [{"profile_id", "decision": "started"|"skipped", "reason", "resets_at"?}]}`.

## Setup
### `ccs auth login [--terminal] | status | logout --profile <id> [--json]`
Claude Code's own sign-in for the profile's config dir ([Profiles & sign-in](02-profiles-and-sign-in.md)). `--profile` is required.

| Subcommand | Does |
|------------|------|
| `login` | Runs `claude auth login --claudeai` for the profile. In a terminal it runs right there; without one (the app) it runs in the background and only the browser opens. `--terminal` opens a new Terminal window for it instead. Waits up to 10 min, then prints the account. Asks a running supervisor to refresh usage. |
| `status` | Signed in or not, the account and plan, and the Keychain item name Claude Code uses (the Keychain itself is never read). |
| `logout` | Signs the profile out of Claude Code after asking `[y/N]` (`--json` skips the question: the caller confirmed). |

Exit codes:
- `status`: 0 whether or not you're signed in, 1 only if `claude` can't be run.
- `login`: 0 when signed in at the end, 1 otherwise, 130 if you cancel with Ctrl-C.
- `logout`: 0 when signed out, 1 if you answer no or it fails.

```sh
$ ccs auth status --profile work --json
{
  "account": "you@example.com",
  "auth_method": "claude.ai",
  "config_dir": "/Users/you/.claude-work",
  "error": null,
  "keychain_service": "Claude Code-credentials-1e91dd84",
  "logged_in": true,
  "ok": true,
  "profile_id": "work",
  "subscription_type": "team"
}
```
`login --json` prints `{ok, profile_id, mode, logged_in, account, subscription_type, daemon_refreshed, error}`, where `mode` is `tty`, `headless` or `terminal`. `logout --json` prints `{ok, profile_id, logged_in, daemon_refreshed, error}`.

### `ccs statusline generate | apply | revert | preview --profile <id> [--json]`
Details in [Statusline](06-statusline.md).

| Subcommand | Does |
|------------|------|
| `generate` | Writes or refreshes `<config dir>/ccs-statusline.py`. `--json`: `{profile_id, script_path, generator_version, changed}` |
| `apply` | Generates the script, backs up `settings.json`, and sets its `statusLine` to the script, so plain `claude` shows it too. It's idempotent (`already_applied`), and refuses an invalid `settings.json` or a profile with `statusline.enabled: false` (exit 1). `--json`: `{profile_id, settings_path, result, backup_path, command}` |
| `revert` | Restores the `statusLine` recorded by `apply`, or removes it if there was none. The script is kept. Result `reverted`, `not_applied`, or `conflict` (you changed `statusLine` since; exit 1, nothing written) |
| `preview` | Prints sample lines for each state. `--json` adds `status: {applied, script_path, script_current}` and each sample's `plain`, `ansi`, and colored `segments` |

`--profile` is required, and an unknown profile exits 2.

### `ccs profile …`
```sh
ccs profile list [--json]
ccs profile show <id> [--json]
ccs profile add --id <id> --flag <flag> --name <name> --emoji <emoji> --config-dir <dir> [--default]
ccs profile set <id> <dotted.key>=<value> [<dotted.key>=<value>…]
ccs profile remove <id> [--default <other>]
```
- `add`: `--flag` defaults to the id and `--name` to the id in title case. `--default` also makes the new profile the default one (used by plain `ccs`); the first profile always becomes the default.
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
| `set` | Changes top-level settings, e.g. `ccs config set default_profile=work display.menu_bar=icon_only`. Profiles are changed with `ccs profile set`; `version` and `revision` are managed automatically. |

- The first command that needs the config creates it with the [seeded profiles](02-profiles-and-sign-in.md#seeded-profiles).
- `profile set`, `profile add`, and `config set` validate the whole config before saving. On errors nothing is written, the command lists each issue with its key path, and exits with 1.
- `validate --json` prints `{"ok": true|false, "issues": [{"path", "message"}], "revision": n, "path": "…"}` and exits 0 when valid, 1 when not (including a missing or unreadable file).
- Successful `--json` output always has `"ok": true`. Errors print `{"ok": false, "error": "…", "issues": […]}`.

### `ccs daemon install | uninstall | start | stop | restart | status | run | logs [--json]`
The background supervisor, a LaunchAgent labelled `local.ccsupervisor.daemon` (`~/Library/LaunchAgents/local.ccsupervisor.daemon.plist`).

| Action | Does |
|--------|------|
| `install` | Writes the LaunchAgent and starts it. It captures your current `PATH` and `HOME` so the supervisor finds `claude`, which means you should re-run it after moving `claude` or `ccs`. The supervisor then starts at login and is restarted if it crashes. |
| `uninstall` | Stops it and removes the LaunchAgent. Your config and state are kept. |
| `start` | Starts the installed supervisor, loading it first if needed (e.g. after `stop`). |
| `stop` | Stops it until the next `start` or login. It stays installed. |
| `restart` | Restarts it. Use this after upgrading `ccs`. |
| `status` | Installed, loaded, responsive, pid, uptime, and socket round-trip time. |
| `logs` | The last 200 lines of `~/.local/state/ccs/logs/daemon.log`. |
| `run` | Runs the supervisor in the foreground. This is what launchd executes; you normally don't. A second copy exits right away with `daemon already running`. |

```
$ ccs daemon status
daemon: running (pid 4242, up 2h 13m, 1.8 ms)
```

### `ccs doctor [--json]`
Checks the whole installation and prints a fix for every problem. It's read-only and takes about a second. Every check is listed in [Troubleshooting](11-troubleshooting.md#start-with-ccs-doctor).
- Exits 1 when any check failed (`✗`), 0 when there are only warnings (`!`).
- `--json`: `{"checks":[{"id","scope","status","message","fix"}],"summary":{"ok","warn","fail"}}`. `scope` is `global` or `profile:<id>`; `status` is `ok`, `warn` or `fail`.

```
$ ccs doctor
Global
  ✓ claude.found: /Users/you/.local/bin/claude
  ✓ daemon.running: running under launchd (pid 4242)
  ! notifications.route: no menu bar app connected: notifications use the osascript fallback
      fix: open -a "CC Supervisor"
  …
Profile 💼 Work (work)
  ✓ auth.status: signed in (claude.ai, team)
  ✓ usage.last_poll: polled 12s ago; data from 12s ago
  …
27 ok · 1 warnings · 0 failed
```

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
