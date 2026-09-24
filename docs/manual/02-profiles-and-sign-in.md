# Profiles & sign-in

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

A **profile** is one Claude Code identity: a config dir (`CLAUDE_CONFIG_DIR`) plus how CC Supervisor shows and supervises it. Widgets, the menu bar, `ccs`, and statuslines all share the same profiles.

## Seeded profiles
On first run, two profiles are created:

| id | Launcher | Name | Emoji | Config dir | Default |
|----|----------|------|-------|------------|---------|
| `personal` | `ccs --personal` | Personal | 🏠 | `~/.claude` | ✓ (`ccs` with no flag) |
| `work` | `ccs --work` | Work | 💼 | `~/.claude-work` | |

Edit or delete them freely.

## Editing profiles in the app
Menu bar → **Settings… → Profiles**. Select a profile, or click **+** to add one.

| Field | Meaning |
|-------|---------|
| ID | Internal identifier (`a–z`, `0–9`, `-`, up to 32 characters). Set once and can't be changed later. |
| Flag | Launcher flag: `work` means `ccs --work`. |
| Name | Shown in widgets, the menu bar, and the statusline. |
| Emoji | Profile icon, used everywhere, including the terminal statusline. |
| Config dir | Claude Code config folder. Pick one with the folder picker; hidden folders are shown. |
| Sign-in | Status, plus **Sign in** / **Sign out** buttons (see below). |
| Limits | Warn and pause thresholds for session, weekly, model-scoped, and extra usage, plus the **Fable warn-only** and **Spill into credits** toggles (see [Limits & supervisor](07-limits-and-supervisor.md)). |
| Supervisor | On/off, and the **resume prompt** typed into paused sessions after a reset. |
| Statusline | On/off, preview, **Apply** / **Revert** (see [Statusline](06-statusline.md)). |
| Warm-up | Model, prompt, triggers, schedule, active hours, cooldown (see [Warm-up](08-warm-up.md)). |

Changes are saved to `~/.config/ccs/config.json`, validated right away (errors show inline), and picked up by the supervisor within about 2 seconds.

## Same thing from the terminal
```sh
ccs profile list
ccs profile show work
ccs profile add --id client --flag client --name "Client X" --emoji 🧪 --config-dir ~/.claude-client
ccs profile set client limits.session.pause=92 warmup.model=haiku
ccs profile set client warmup.triggers.schedule='[{"time":"06:00","weekdays":["mon","tue","wed","thu","fri"]}]'
ccs profile remove client
```
- `set` takes dotted keys. Values are read as JSON when valid, otherwise as plain strings.
- `remove` deletes only the profile entry in CC Supervisor. It doesn't delete the config dir or sign you out.
- The default profile is `default_profile` in the [config](10-configuration-reference.md).

## Flag rules
- Must be unique across profiles.
- Must not be a reserved name:
  - ccs's own options: `help`, `version`, `profile`, `force`, `no-supervise`, `json`
  - any `claude` long option, for example `model`, `resume`, `continue`, `print`, `effort`. `ccs doctor` refreshes this list from `claude --help`.
- The profile flag must be the **first** argument: `ccs --work --model opus` works; `ccs --model opus --work` doesn't.

## Config dir rules
- Each profile needs its own config dir. Two profiles can't share one.
- Write it as an absolute path or starting with `~`, for example `~/.claude-work`.
- The dir doesn't need to exist yet. Signing in creates what Claude Code needs.

## Signing in (via claude.ai)
- **App:** Settings → Profiles → *profile* → **Sign in**, or click a widget that says *Sign in required*.
- **Terminal:** `ccs auth login --profile work`

What happens:
1. Claude Code's own login starts for that profile's config dir, and your browser opens claude.ai.
2. You approve. Claude Code stores the login in the macOS Keychain:
   - service `Claude Code-credentials` for `~/.claude`
   - `Claude Code-credentials-<8 hex>` for other dirs
3. The supervisor refreshes usage right away, and widgets show data within about a minute.

Notes:
- If the login needs a terminal, the app opens a terminal window running `ccs auth login --profile <id>`. Finish the steps there. (Whether this is needed is still being verified.)
- The login is **shared with plain Claude Code**: after signing in here, `CLAUDE_CONFIG_DIR=~/.claude-work claude` is signed in too, and the other way round.
- CC Supervisor stores **no tokens, cookies, or passwords**.

## Checking sign-in status
```sh
ccs auth status --profile work
```
The app shows the same status per profile. When a profile's login expires or is missing:
- widgets and the menu bar show **Sign in required**
- you get a notification
- warm-ups for that profile are skipped

## Signing out
- **App:** Settings → Profiles → *profile* → **Sign out**.
- **Terminal:** `ccs auth logout --profile work`

This signs that config dir out of Claude Code itself, so plain `claude` with that dir is signed out too. You'll be asked to confirm.
