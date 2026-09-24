# Installation

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

CC Supervisor is built from source on your Mac for personal use. There is no App Store version and no download.

## Prerequisites
| Requirement | Check | Install |
|-------------|-------|---------|
| macOS 26 (Tahoe) or later | `sw_vers` | – |
| Xcode 26 (full Xcode, not only the Command Line Tools) | `xcodebuild -version` | App Store |
| Python ≥ 3.12 | `python3 --version` | pyenv / Homebrew |
| pipx | `pipx --version` | `brew install pipx` |
| XcodeGen | `xcodegen --version` | `brew install xcodegen` |
| Claude Code, signed in or ready to sign in | `claude --version` | [claude.com/claude-code](https://claude.com/claude-code) |

## Install
From the repo root:
```sh
make install
```
This does the following:
1. Installs the `ccs` CLI with pipx (an editable install, so pulling the repo updates `ccs`).
2. Generates the Xcode project with XcodeGen and builds the app and widgets with `xcodebuild`.
3. Copies `CC Supervisor.app` to `~/Applications/`.
4. Installs and starts the background supervisor (`ccs daemon install`).
5. Opens the app.

### What goes where
| Thing | Location |
|-------|----------|
| `ccs` command | `~/.local/bin/ccs` |
| App + widgets | `~/Applications/CC Supervisor.app` |
| Background supervisor | LaunchAgent `~/Library/LaunchAgents/local.ccsupervisor.daemon.plist` |
| Your settings | `~/.config/ccs/config.json` |
| Runtime data (usage, sessions, logs) | `~/.local/state/ccs/` |
| Statusline script (per profile) | `<config dir>/ccs-statusline.py`, for example `~/.claude-work/ccs-statusline.py` |

Make sure `~/.local/bin` is on your `PATH` (`pipx ensurepath`).

The daemon's LaunchAgent remembers the `PATH` you had when you ran `ccs daemon install`, so it can find `claude`. If you later move Claude Code, run `ccs daemon install` again, or set `claude_path` in the [config](10-configuration-reference.md).

## First launch
- The app lives in the **menu bar** only. It has no Dock icon.
- macOS asks for **notification permission**. Allow it so you get limit warnings (see [Notifications](09-notifications.md)).
- **Launch at login** is offered in **Settings… → General**. Keep it on: the app forwards app-start and unlock/wake warm-up triggers and shows notifications.
- **Sign in** each profile ([Profiles & sign-in](02-profiles-and-sign-in.md)).
- **Add widgets:**
  1. Right-click the desktop → **Edit Widgets…**
  2. Search "CC Supervisor" and pick small, medium, or large.
  3. Right-click the placed widget → **Edit Widget** → choose the profile.

## Signing (local builds)
- Builds are signed "to run locally" (ad-hoc). No Apple ID or developer account is needed.
- *May apply, pending a technical check (P00-S1):* if macOS refuses to load widgets from an ad-hoc build, the build uses a **free Apple ID** team in Xcode instead. Free signing expires after **7 days**, so you'd need to rebuild weekly:
  ```sh
  make app
  ```
  This page will say which variant applies once it is verified.

## Upgrade
```sh
make upgrade
```
It:
- reinstalls `ccs` (pipx)
- rebuilds and reinstalls the app
- restarts the supervisor
- regenerates every profile's statusline script
- runs `ccs doctor`

`ccs --<profile>` also regenerates an outdated statusline script by itself. If `ccs doctor` still reports one, for example for a profile you only use with plain `claude`, run `ccs statusline generate --profile <id>`.

## Uninstall
```sh
make uninstall
```
- Reverts the statusline in every profile's `settings.json` from the backup made at apply time.
- Stops and removes the LaunchAgent.
- Removes `~/Applications/CC Supervisor.app` and uninstalls `ccs` from pipx.
- **Asks** before deleting `~/.config/ccs/` (your settings) and `~/.local/state/ccs/` (runtime data). Answer "no" to keep them for a later reinstall.
- **Never** touches your Claude Code logins, conversations, or other settings in your config dirs.
