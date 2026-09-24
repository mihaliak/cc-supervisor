# Profiles & sign-in

> Status: shipped (2026-09-24). The GUI parts (menu bar, Settings, widgets, native notifications) haven't had a human check yet.

A **profile** is one Claude Code identity: a config dir (`CLAUDE_CONFIG_DIR`) plus how CC Supervisor shows and supervises it. Widgets, the menu bar, `ccs`, and statuslines all share the same profiles.

## Seeded profiles
On first run (the first `ccs` command that needs the config), the config file is created with these profiles. A profile is only seeded if its config dir already exists; if neither does, only `personal` is created. The first seeded profile becomes the default.

| id | Launcher | Name | Emoji | Config dir | Default |
|----|----------|------|-------|------------|---------|
| `personal` | `ccs --personal` | Personal | 🏠 | `~/.claude` | ✓ (`ccs` with no flag) |
| `work` | `ccs --work` | Work | 💼 | `~/.claude-work` | |

Edit or delete them freely.

## Editing profiles in the app
Menu bar → **Settings… → Profiles** (or the ⚙ on a profile card, or click a widget). The list shows each profile's emoji, name, launcher flag, a sign-in dot (green = signed in, orange = signed out) and a red ⚠ when it has invalid settings.

| Section | Fields |
|---------|--------|
| Identity | **ID** (read-only: set once when the profile is added), **Launcher flag** (`work` → `ccs --work`, shown live), **Name**, **Emoji** (type one, or click 😀 for the emoji palette), **Claude config dir** (type a path and press Return, or **Choose…**; hidden folders are shown, paths in your home are stored as `~/…`), **Default profile** (**Make default** sets `default_profile`). |
| Account | Sign-in status, account, subscription and the Keychain item name, plus **Sign in…** / **Sign out…** (see below). |
| Limits | Warn and pause thresholds (1–100 %) for session, weekly, model-scoped and extra usage, plus the **Fable warn-only** and **Spill into credits** toggles (see [Limits & supervisor](07-limits-and-supervisor.md)). |
| Supervisor | On/off, and the **resume prompt** typed into interrupted sessions after a reset. |
| Statusline | On/off, a colored preview of every state, **Apply to settings.json** / **Revert** (see [Statusline](06-statusline.md)). |
| Warm-up | On/off, model, prompt, triggers, scheduled times, active hours, cooldown, plus the next and last warm-up (see [Warm-up](08-warm-up.md)). |

How saving works:
- Changes are written to `~/.config/ccs/config.json` about half a second after you stop typing (and when you close the window), in exactly the format `ccs` writes. Keys the app doesn't know are kept.
- After each save the app runs `ccs config validate`. Problems show in red under the field, and a banner says the config is invalid. Invalid values stay in the file until you fix them; the supervisor keeps using the last valid config meanwhile.
- The supervisor picks up valid changes within about 2 seconds.
- If `ccs` (or another window) changes the same setting while you're editing, the app asks: **Keep mine** or **Take theirs**. Changes to other settings are merged automatically.
- If there's no config file yet, a banner offers **Create config** (the same as running `ccs profile list`, which creates the default profiles).

**Add** (the **+** under the list): enter ID, launcher flag (defaults to the ID), name, emoji, config dir, and optionally make it the default. The app runs `ccs profile add`, so every other setting starts from the defaults.

**Remove** (the **−**): after you confirm, the app runs `ccs profile remove`. Only the CC Supervisor profile is removed; the Claude config dir and its sign-in stay. If it was the default profile, the next profile becomes the default.

## Same thing from the terminal
```sh
ccs profile list
ccs profile show work
ccs profile add --id client --flag client --name "Client X" --emoji 🧪 --config-dir ~/.claude-client
ccs profile set client limits.session.pause=92 warmup.model=haiku
ccs profile set client warmup.triggers.schedule='[{"time":"06:00","weekdays":["mon","tue","wed","thu","fri"]}]'
ccs profile remove client
```
- `add`: `--flag` defaults to the id, `--name` to the id in title case (`client-x` → `Client X`). `--default` also makes it the default profile; the very first profile always becomes the default.
- `set` takes dotted keys. Values are read as JSON when valid, otherwise as plain strings. The `id` can't be changed.
- `remove` deletes only the profile entry in CC Supervisor. It doesn't delete the config dir or sign you out. Removing the default profile while others exist needs `--default <other>`.
- The default profile is `default_profile` in the [config](10-configuration-reference.md). Change it with `ccs config set default_profile=work`.
- Every change is validated before it is saved. If it's invalid, nothing is written and the errors are listed with their key paths.

## Flag rules
- Lowercase letters, digits, and `-`, starting with a letter or digit, at most 32 characters (the same rule as the id).
- Must be unique across profiles.
- Must not be a reserved name:
  - ccs's own options: `help`, `version`, `profile`, `force`, `no-supervise`, `json`
  - any `claude` long option, for example `model`, `resume`, `continue`, `print`, `effort`. `ccs doctor` refreshes this list from `claude --help`.
- The profile flag goes **in front**, among ccs's own options (`--force`, `--no-supervise`, in any order): `ccs --work --model opus` and `ccs --force --work -c` work. In `ccs --model opus --work`, the `--work` is passed to `claude`, and the default profile is used.
- `ccs` with no profile flag uses the default profile. If there is no default, it uses the only profile, or asks you to pass a flag.

## Config dir rules
- Each profile needs its own config dir. Two profiles can't share one.
- Write it as an absolute path or starting with `~`, for example `~/.claude-work`.
- The dir doesn't need to exist yet. Signing in creates what Claude Code needs.
- For the default `~/.claude`, `ccs` doesn't set `CLAUDE_CONFIG_DIR` at all, so Claude Code keeps using `~/.claude.json` for its global state, just like plain `claude`.

## Signing in (via claude.ai)
- **App:** Settings → Profiles → *profile* → Account → **Sign in…** (**Sign in again…** when already signed in), or click a widget that says *Sign in required*. While it waits for the browser, **Cancel** stops it.
- **Terminal:** `ccs auth login --profile work`

What happens:
1. Claude Code's own login (`claude auth login --claudeai`) starts for that profile's config dir, and your browser opens claude.ai.
2. You approve in the browser. Claude Code stores the login in the macOS Keychain:
   - service `Claude Code-credentials` for `~/.claude`
   - `Claude Code-credentials-<8 hex>` for other dirs
3. `ccs` checks the result and prints who you're signed in as, for example `Signed in 💼 Work as you@example.com · team.`
4. If the supervisor is running, it refreshes usage right away, and widgets show data within about a minute.

Where the sign-in runs:
- **From a terminal** (`ccs auth login` typed by you): in that terminal. You see Claude Code's own messages, and Ctrl-C cancels.
- **From the app:** in the background. Only your browser opens; the app waits up to 10 minutes for you to finish.
- **`ccs auth login --terminal`:** opens a new Terminal window that runs `ccs auth login --profile <id>`. Use it if the browser sign-in from the app doesn't work for you. The window's small script lives in `~/.local/state/ccs/tmp/` only while you sign in and is deleted afterwards; it holds no secrets.

Notes:
- Always sign in to the claude.ai account whose limits this profile should track. The status (below) shows the account, so you can spot a wrong one.
- The login is **shared with plain Claude Code**: after signing in here, `CLAUDE_CONFIG_DIR=~/.claude-work claude` is signed in too, and the other way round.
- CC Supervisor stores **no tokens, cookies, or passwords**, and never reads the Keychain.

## Checking sign-in status
```sh
ccs auth status --profile work
```
```
💼 Work (~/.claude-work)
  signed in as you@example.com · team
  keychain item: Claude Code-credentials-1e91dd84
```
The app shows the same status per profile. When a profile's login expires or is missing:
- widgets and the menu bar show **Sign in required**
- you get a notification (at most once an hour per profile)
- warm-ups for that profile are skipped

## Signing out
- **App:** Settings → Profiles → *profile* → Account → **Sign out…** (the app asks you to confirm).
- **Terminal:** `ccs auth logout --profile work`, which asks `Log out 💼 Work (~/.claude-work)? [y/N]`.

This signs that config dir out of Claude Code itself, so plain `claude` with that dir is signed out too.
