# Troubleshooting

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

## Start with `ccs doctor`
```sh
ccs doctor
```
It runs every check below in parallel (about a second), prints `✓` / `!` / `✗` per check grouped by global and per profile, and a `fix:` line under every warning or failure. It exits 1 when any check failed.
- **Read-only:** it never creates the config, never writes state, and never changes a `settings.json`. It runs `claude --version`, `claude --help`, and `claude auth status` per profile.
- Each check has a deadline: a hung `claude` or socket shows up as a failed check ("timed out"), never as a hang.
- `ccs doctor --json` prints `{"checks":[{"id","scope","status","message","fix"}],"summary":{"ok","warn","fail"}}` for scripts. Settings › General › **Run diagnostics** shows the same list.

### Global checks
| Check | ✗ / ! means | Fix |
|-------|-------------|-----|
| `python.version` | ✗ Python older than 3.12 runs `ccs` | `make install-dev` |
| `ccs.version` | always ✓: version and the source folder of this `ccs` (an editable install points into the repo) | – |
| `claude.found` | ✗ `claude` isn't on `PATH`, or `claude_path` points nowhere | install Claude Code, or `ccs config set claude_path=/path/to/claude` |
| `claude.daemon_path` | ✗ the `PATH` saved in the LaunchAgent can't find `claude` (only shown when the daemon is installed and `claude_path` is unset) | `ccs daemon install` (re-saves your current `PATH`) |
| `claude.version` | ! `claude --version` failed | check that `claude` runs |
| `config.valid` | ✗ no config file, or it has errors (the first three are shown) | `ccs profile list` creates it; `ccs config validate` lists every error |
| `config.reserved_flags` | ✗ a profile flag is now also a `claude` option (e.g. a new Claude Code release added `--work`); ! `claude` has options `ccs` doesn't know yet | `ccs profile set <id> flag=<new-flag>`; update CC Supervisor |
| `daemon.installed` | ✗ no LaunchAgent | `ccs daemon install` |
| `daemon.running` | ✗ the LaunchAgent isn't running, or launchd shows it loaded with no live process | `ccs daemon start`; `ccs daemon logs`, then `ccs daemon restart`. ✓ "running outside launchd" when you started `ccs daemon run` by hand |
| `daemon.socket` | ✗ the daemon doesn't answer on its socket; ! slower than 1 s, or it runs another `ccs` version than this CLI | `ccs daemon restart` |
| `notifications.route` | ! no menu bar app is connected, so notifications fall back to osascript (shown as "Script Editor") | `open -a "CC Supervisor"` |
| `app.installed` | ! `~/Applications/CC Supervisor.app` is missing | `make app` |
| `widget.snapshot` | ! no widget data yet, or older than 5 minutes (widgets then show "Supervisor offline") | `ccs daemon start` / `ccs daemon restart` |

### Per-profile checks
| Check | ✗ / ! means | Fix |
|-------|-------------|-----|
| `config_dir.exists` | ✗ the profile's config dir doesn't exist | create it, or `ccs profile set <id> config_dir=<dir>` |
| `auth.status` | ! not signed in, or `claude auth status` couldn't be read | `ccs auth login --profile <id>` |
| `auth.keychain_service` | always ✓: the Keychain item name Claude Code uses for this dir (never read) | – |
| `usage.last_poll` | ! no usage data yet; the last poll attempt is over 5 min old (the daemon isn't polling); or the last *successful* poll is over 10 min old (polls fail; the error is shown) | `ccs daemon start` / `ccs daemon restart`; `ccs usage --refresh --profile <id>` |
| `usage.status` | ! sign-in required, no subscription, or stale data; ✗ usage source error (Claude Code changed its experimental usage interface) | `ccs auth login --profile <id>`; `ccs usage --refresh --profile <id>`; update CC Supervisor |
| `statusline.script` | ! the script is outdated (older generator or code, or the profile's name, emoji, thresholds or Python changed). ✓ when not generated yet: `ccs --<flag>` creates it | `ccs statusline generate --profile <id>` |
| `statusline.interpreter` | ✗ the Python the script runs with is gone (e.g. after a Homebrew or pyenv upgrade) | `ccs statusline generate --profile <id>`, then `ccs statusline apply --profile <id>` if applied |
| `statusline.applied` | ! applied with an outdated command. ✓ when not applied or when `settings.json` has another `statusLine` (informational: `ccs` sessions still show theirs) | `ccs statusline apply --profile <id>` |
| `supervisor.state` | ! a pause should have been lifted more than 10 minutes ago (the supervisor isn't running or can't confirm the reset), or the state file is unreadable | `ccs daemon restart`, or `ccs resume --profile <id>` |

## Logs and history
```sh
ccs daemon logs          # recent supervisor log lines (~/.local/state/ccs/logs/daemon.log)
ccs events               # warnings, pauses, resumes, warm-ups, errors
ccs events --follow      # live
ccs status               # current usage + supervisor state
```

## The background supervisor
```sh
ccs daemon status        # installed? loaded? responding? pid, uptime
ccs daemon restart       # restart it (e.g. after upgrading ccs)
ccs daemon install       # (re)install: rewrites the LaunchAgent with your current PATH
```
- `ccs daemon status` says **loaded but not responding**: look at `ccs daemon logs`. launchd's own output for crashes that happen before logging starts is in `~/.local/state/ccs/logs/launchd.err.log`.
- To run it by hand and watch its log, stop the LaunchAgent first (`ccs daemon stop`), then run `ccs daemon run --foreground`. A second copy exits immediately with `daemon already running`.
- Low-level launchd checks: `launchctl print gui/$(id -u)/local.ccsupervisor.daemon` shows state and pid; `launchctl kickstart -k gui/$(id -u)/local.ccsupervisor.daemon` restarts it.
- The supervisor rewrites `~/.local/state/ccs/usage/<profile>.json` and `widget/snapshot.json` after every usage check, even a failed one. A file older than 5 minutes means the supervisor isn't running, which is what `⚠ supervisor offline` in the statusline and widgets reports.

## Common problems
| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Widget doesn't update or shows old numbers | macOS throttles widget refreshes | Normal. The menu bar is live. If greyed with "Updated HH:MM", data is stale: see "supervisor offline". |
| Widget or menu bar says **Sign in required** | The profile's Claude Code login is missing or expired | Click it → **Sign in**, or `ccs auth login --profile <id>` |
| Statusline shows `⚠ supervisor offline` | The background supervisor isn't running or isn't installed | `ccs daemon start`. If not installed: `ccs daemon install`. Check `ccs daemon logs`. |
| Statusline shows `?%` | No usage data yet, or older than 10 minutes | Wait a minute after sign-in. Otherwise check `ccs status` and `ccs daemon logs`. |
| No statusline in plain `claude` | It's only on automatically in `ccs` sessions | `ccs statusline apply --profile <id>` |
| No statusline in a `ccs` session either | Statusline disabled for the profile, or hooks/statusline disabled in your Claude settings | Check `statusline.enabled`, then `ccs statusline preview --profile <id>` |
| Statusline shows the old format or an old name or emoji | Script generated by an older version or config | `ccs statusline generate --profile <id>`. `ccs` sessions do this automatically, and the supervisor refreshes existing scripts. |
| Statusline shows only `💼 Work ~ ?%` | The script hit an unexpected error and printed its fallback line | `ccs statusline generate --profile <id>`. If CC Supervisor's Python moved (for example after a pyenv or Homebrew upgrade), also re-run `ccs statusline apply --profile <id>`. |
| `ccs statusline revert` reports a conflict | You changed `statusLine` in `settings.json` after applying | Edit `settings.json` by hand. The previous value is in `~/.local/state/ccs/statusline/<profile>.json`. |
| `ccs statusline apply` fails with `invalid_settings` | `<config dir>/settings.json` isn't valid JSON | Fix the file (it's never modified while invalid), then apply again. |
| Session wasn't interrupted at the pause threshold | It was idle (only marked paused); it isn't a `ccs` session (background agents and plain `claude` aren't supervised); it was already overridden this window; or data was stale | `ccs sessions` shows supervision state; `ccs events` shows what happened |
| Paused session didn't continue after the reset | It was idle when paused (no prompt needed), you overrode it, or the reset isn't confirmed yet | Type your prompt, or `ccs resume --profile <id>`. Resume is forced 10 min after the reset time if data can't confirm it. |
| Warm-up didn't run | A skip rule applied: window already active, session busy, cooldown, outside active hours, weekly pause, sign-in required, trigger off | `ccs events` shows the skip reason. Force with `ccs warmup --profile <id> --force`. |
| Menu shows **ccs not found at …** | The app can't find the `ccs` command (GUI apps don't see your shell `PATH`) | Install it (`make install-dev`) or set `ccs_path` in the config / Settings |
| Menu shows **Supervisor daemon not installed / stopped** | The background supervisor isn't running | Click **Install daemon** / **Start daemon** in the banner, or `ccs daemon install` / `ccs daemon start` |
| Menu bar shows `?%` | No session data for that profile yet (just installed, or sign-in required) | Wait for the first poll, or sign in (card → **Sign in**) |
| "updated … ago" in the menu header is orange | The supervisor hasn't written data for over 5 minutes | `ccs daemon status`; restart with `ccs daemon restart` |
| Unlock/app-start warm-ups never happen | The app isn't running, or the supervisor was offline when the event happened (the app only forwards triggers while connected) | Start the app; enable Settings → General → Launch at login; check `ccs daemon status` |
| Notifications come from "Script Editor" | The app isn't running, so the fallback is used | Start the app |
| No notifications at all | Permission denied or toggles off | System Settings → Notifications → CC Supervisor; Settings → General → Notifications |
| `ccs --myflag` runs the default profile or errors | Flag unknown, reserved, or not in front of the Claude arguments | `ccs profile list`; put the profile flag first |
| `ccs: supervisor not installed — running unsupervised` | The LaunchAgent isn't installed | `ccs daemon install`. The session still works, but it can't be paused. |
| `ccs: supervisor not responding — running unsupervised` | Installed, but it didn't answer after being started | `ccs daemon status`, `ccs daemon logs`. The session registers by itself once the supervisor answers. |
| Terminal left in a weird state after a crash (no echo, odd line breaks) | The session was killed hard (e.g. `kill -9` of `ccs`) before it could restore your terminal | Type `reset` and press Enter. A normal exit, `kill`, or closing the window always restores it. |
| Debugging whether supervision causes a problem | — | Run `ccs --work --no-supervise …`: same session, no supervisor. For a completely plain run: `CLAUDE_CONFIG_DIR=~/.claude-work claude` (for `~/.claude`, just `claude`). |
| "config invalid" notification | Hand edit broke `config.json` | `ccs config validate` shows which key; the last valid config keeps running |
| `ccs usage` says `no data yet` | The supervisor hasn't polled this profile yet, or isn't running | `ccs usage --refresh` asks Claude Code directly; `ccs daemon status` |
| `ccs usage` says `no plan limits` | The profile is signed in with an API key, or the account has no Claude subscription | Nothing to track. Sign in with a subscription account: `ccs auth login --profile <id>` |
| Usage error: **usage source error** | Claude Code changed how it reports usage (that interface is experimental), or `claude` couldn't start | Update CC Supervisor; `ccs usage --refresh --profile <id>` shows the error text directly; `ccs doctor`; `ccs daemon logs`. The last good numbers stay visible, and **no pauses happen** until fixed. |
| Supervisor can't find `claude` | `PATH` changed since `ccs daemon install` | Re-run `ccs daemon install`, or set `claude_path` in the config |
| `ccs status` first line says `daemon: not running` | The supervisor is stopped or not installed; the numbers shown are the last saved ones | `ccs daemon start`, or `ccs daemon install` |
| `ccs daemon start` says "not installed" | The LaunchAgent was never installed or was uninstalled | `ccs daemon install` |
| Widgets missing from the gallery | App not in `~/Applications` or never opened; build signing (see [Installation](01-installation.md#signing-local-builds)) | Open the app once; rebuild with `make app` |
| Ctrl-Z in a `ccs` session | `ccs` handles Ctrl-Z itself: it suspends the whole session and returns you to the shell | `fg` brings it back and repaints the screen |
| Sign in opens the browser but nothing happens | The browser flow didn't reach Claude Code's local callback (e.g. the tab was closed) | Retry **Sign in**, or run `ccs auth login --profile <id> --terminal` |
| Sign in fails with "timed out after 600 s" | The browser step wasn't finished within 10 minutes | Retry and complete the claude.ai approval in the browser |
| No browser or Terminal window opens for sign-in | `open` couldn't launch the browser or Terminal (for example, run over SSH) | Run `ccs auth login --profile <id>` in a local terminal; Claude Code prints the sign-in URL there |
| Signed in to the wrong account | The browser was logged into a different claude.ai account | `ccs auth status --profile <id>` shows the account. `ccs auth logout --profile <id>`, switch accounts on claude.ai, then sign in again |

## Reset or start over
```sh
ccs daemon restart                       # restart the supervisor
ccs statusline revert --profile <id>     # undo statusline apply for a profile
make uninstall                           # remove everything (asks before deleting config/state)
```
To reset only runtime data (not settings):
```sh
ccs daemon stop
rm -rf ~/.local/state/ccs
ccs daemon start
```
Active pauses are forgotten after this.
