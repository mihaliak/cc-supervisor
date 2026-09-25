# Installation

> Status: shipped (2026-09-24). The GUI parts (menu bar, Settings, widgets, native notifications) haven't had a human check yet.

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
1. Checks the prerequisites (`make prereqs`). Anything missing is printed with the command that installs it, and the install stops.
2. Installs the `ccs` CLI with pipx (an editable install, so pulling the repo updates `ccs`).
3. Generates the Xcode project with XcodeGen, builds the app and widgets with `xcodebuild`, and copies `CC Supervisor.app` to `~/Applications/`.
4. Creates `~/.config/ccs/config.json` if it doesn't exist yet, with `personal` (`~/.claude`) and `work` (`~/.claude-work`) for the dirs that exist. An existing config is kept as is.
5. Installs and starts the background supervisor (`ccs daemon install`).
6. Turns on **Launch at login** for the app and opens it.
7. Waits up to 20 s for the first usage poll, then runs `ccs doctor` and prints the next steps.

`make install` **never touches your Claude config dirs** (`~/.claude*`): no statusline script is written and no `settings.json` is changed. `ccs --<flag>` creates the statusline script on first use; `ccs statusline apply` is a separate, explicit step (see [Statusline](06-statusline.md)).

Running `make install` again is safe: it reinstalls the CLI and the app, keeps your config, and reinstalls the LaunchAgent.

After installing, review the new profiles in **Settings… → Profiles**: warm-ups are on by default (app start, unlock/wake, auto-chain), with Haiku and the prompt `Reply with just: ok`.

### What goes where
| Thing | Location |
|-------|----------|
| `ccs` command | `~/.local/bin/ccs` |
| App + widgets | `~/Applications/CC Supervisor.app` |
| Background supervisor | LaunchAgent `~/Library/LaunchAgents/local.ccsupervisor.daemon.plist` |
| Your settings | `~/.config/ccs/config.json` |
| Runtime data (usage, sessions, logs) | `~/.local/state/ccs/` |

If your shell sets `XDG_CONFIG_HOME`, `XDG_STATE_HOME` or `CCS_STATE_DIR`, `ccs daemon install` saves them in the LaunchAgent, and the app reads them from there when it launches, so the app, the supervisor and `ccs` use the same folders. Restart the app after reinstalling the daemon with different folders. Widgets can only read `~/.local/state/ccs/`, so they need the default state folder (`ccs doctor` warns otherwise).
| Statusline script (per profile) | `<config dir>/ccs-statusline.py`, for example `~/.claude-work/ccs-statusline.py` |

Make sure `~/.local/bin` is on your `PATH` (`pipx ensurepath`).

The daemon's LaunchAgent remembers the `PATH` you had when you ran `ccs daemon install`, so it can find `claude`. If you later move Claude Code, run `ccs daemon install` again, or set `claude_path` in the [config](10-configuration-reference.md).

Check that everything is up:
```sh
ccs doctor          # every check with a fix hint; see Troubleshooting
ccs daemon status   # daemon: running (pid …, up …)
ccs status          # usage per profile, as the supervisor sees it
```

## First launch
- The app lives in the **menu bar** only. It has no Dock icon.
- macOS asks for **notification permission**. Allow it so you get limit warnings (see [Notifications](09-notifications.md)).
- **Launch at login** is off until you turn it on in **Settings… → General** (`make install` turns it on for you). Keep it on: the app forwards app-start and unlock/wake warm-up triggers and shows notifications.
- If the dropdown shows a **Supervisor daemon not installed** banner, click **Install daemon** (same as `ccs daemon install`).
- **Sign in** each profile ([Profiles & sign-in](02-profiles-and-sign-in.md)).
- **Add widgets:**
  1. Right-click the desktop → **Edit Widgets…**
  2. Search "CC Supervisor" and pick small, medium, or large.
  3. Right-click the placed widget → **Edit Widget** → choose the profile.

## Signing (local builds)
- Builds are signed "to run locally" (ad-hoc). No Apple ID or developer account is needed.
  - Verified: macOS registers and launches ad-hoc widget extensions, and grants their read-only access to `~/.local/state/ccs/widget/`.
- **Only if** widgets ever fail to show up after `make app` and opening the app once: the fallback is a **free Apple ID** team in Xcode with an App Group. Free signing expires after **7 days**, so you'd rebuild weekly with `make app`.
- After uninstalling, macOS keeps a small empty sandbox container at `~/Library/Containers/local.ccsupervisor.app.widgets`. This is normal and harmless.

## Upgrade
```sh
make upgrade
```
It:
- reinstalls `ccs` (pipx)
- rebuilds and reinstalls the app
- reinstalls the supervisor's LaunchAgent (`ccs daemon install`, so it picks up new LaunchAgent settings) and restarts the menu bar app on the new code
- regenerates the statusline scripts that already exist (it doesn't create new ones)
- runs `ccs doctor`

`ccs --<profile>` also regenerates an outdated statusline script by itself. If `ccs doctor` still reports one, for example for a profile you only use with plain `claude`, run `ccs statusline generate --profile <id>`.

## For developers
`make help` lists these targets:

| Target | Does |
|--------|------|
| `make venv` | Creates `python/.venv` with the dev tools: pytest, ruff, mypy. |
| `make install-dev` | Editable pipx install of `ccs` into `~/.local/bin`. It uses the real interpreter behind any pyenv shim. |
| `make test` | Python tests plus Swift unit tests. `SKIP_SWIFT=1` skips Swift. |
| `make test-python` / `make test-swift` | One half only. |
| `make test-live` | Tests marked `live`, which use the real `claude`. Opt-in only. |
| `make lint` / `make fmt` | ruff check, format check, and mypy `--strict` / auto-format. |
| `make project` | Generates `macos/CCSupervisor.xcodeproj` with XcodeGen. It is not committed. |
| `make app-build` | Builds the app and widgets (Release, ad-hoc signed) into `build/xcode`. |
| `make app` | `app-build`, then copies the app to `~/Applications/`. |
| `make clean` | Removes build outputs, caches, and the generated project. |
| `make prereqs` | Checks the install prerequisites (also the first step of `make install`). |
| `make install` / `make upgrade` / `make uninstall` | See the sections on this page. |

## Uninstall
```sh
make uninstall
```
- Asks for confirmation first.
- Restores the previous `statusLine` everywhere `ccs statusline apply` set one, working from the records in `~/.local/state/ccs/statusline/` (so removed profiles are covered too). If `ccs` can't run, it restores them with plain `python3`. If you changed that `statusLine` since, it reports a conflict and leaves the file alone.
- Finds `ccs` in `~/.local/bin` or on your `PATH`.
- Stops and removes the LaunchAgent.
- Turns off Launch at login, quits the app, and removes `~/Applications/CC Supervisor.app`.
- Uninstalls `ccs` from pipx.
- **Asks** before deleting `~/.config/ccs/` (your settings), `~/.local/state/ccs/` (runtime data), and the generated `ccs-statusline.py` files in your config dirs. Answer "no" to keep them for a later reinstall.
- **Never** touches your Claude Code logins, conversations, or other settings in your config dirs.

For scripted runs: `make uninstall YES=1` answers the first question with yes and keeps config and state; add `PURGE=1` to delete them too. A record of a `statusLine` that couldn't be restored is never deleted, even with `PURGE=1`; its path is printed so you can fix `settings.json` by hand.

If an old login item stays behind (for example after deleting the app by hand), remove it in **System Settings › General › Login Items**.
