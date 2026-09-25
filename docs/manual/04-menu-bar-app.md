# Menu bar app

> Status: shipped (2026-09-24). The GUI parts (menu bar, Settings, widgets, native notifications) haven't had a human check yet.

`CC Supervisor.app` runs in the menu bar only (no Dock icon). It powers the widgets, shows notifications, forwards app-start and unlock/wake events for [warm-ups](08-warm-up.md), and holds the Settings window. All data and actions come from `ccs`. The app itself makes no decisions.

## Menu bar label
Set in Settings → General (`display.menu_bar`):

| Mode | Looks like |
|------|------------|
| Letter + session/weekly % (default) | `P 2% ● 63% ●   W 24% ● 99% ●`: per profile, the first letter of its name, then session % and a dot, then weekly % and a dot. Only the dots are colored green/yellow/red (gray = no data); `?%` means no data yet (e.g. sign-in required). |
| Icon only | a single gauge icon tinted by the worst level across all profiles |

The label uses a smaller font than the rest of the menu bar. It is drawn as an image so the dot colors survive; the letters and numbers follow the menu bar's light or dark appearance. Configs that still say `emoji_percent` show this letter label.

## Dropdown
```
┌──────────────────────────────────────────────┐
│ CC Supervisor                 updated 1m ago │
│ ┌──────────────────────────────────────────┐ │
│ │ 💼 Work                      ⏸ Paused    │ │
│ │ Session                           90%    │ │
│ │ ██████████████████████████████░░░        │ │
│ │ 20:00 · in 42m                           │ │
│ │ Weekly                            61%    │ │
│ │ ███████████████████░░░░░░░░░░░░░         │ │
│ │ Sat 08:00 · in 1d 13h                    │ │
│ │ Fable                             12%    │ │
│ │ Extra usage                       32%    │ │
│ │ €3.20 / €10.00                           │ │
│ │ 3 ccs sessions · 2 paused · 1 other      │ │
│ │ Resumes 20:00 · in 42m                   │ │
│ │ Next warm-up 06:00                       │ │
│ │ [Warm up now] [Resume]               ⚙   │ │
│ └──────────────────────────────────────────┘ │
│ ┌ 🏠 Personal … ───────────────────────────┐ │
│ ──────────────────────────────────────────── │
│ ⟳ Refresh                  Settings…   Quit  │
└──────────────────────────────────────────────┘
```
- **Header**: when the data was last written by the supervisor. It turns orange when that is more than 5 minutes ago (supervisor offline).
- **Per-profile card:**
  - A status line when something needs attention: `updated 14m ago` (stale data), **Sign-in required** with a **Sign in** button, *No Claude subscription limits*, *Usage unavailable (see ccs doctor)*, or *No data yet*.
  - Session, weekly, model-scoped (for example Fable), and extra usage rows, each with percent, colored bar, and reset time (`20:00 · in 42m`). Extra usage shows the spend (`€3.20 / €10.00`) instead of a reset time.
  - Supervisor line: how many `ccs` sessions are active and paused, how many **other** sessions exist (background agents and plain `claude`, not supervised), when paused sessions resume, and the next warm-up time.
  - **Warm up now**: start a session window for this profile (`ccs warmup --profile <id> --trigger manual`). The normal [skip rules](08-warm-up.md#skip-rules) still apply; the result (started or skipped, with the reason) shows on the card for a few seconds.
  - **Pause** / **Resume** (label follows the profile's state): manually pause or resume that profile's `ccs` sessions (`ccs pause|resume --profile <id>`).
  - **⚙**: opens Settings on this profile.
  - Errors from any action show in red on the card.
- **Refresh**: fetch usage for all profiles now (through the supervisor, or `ccs usage --refresh` when it's offline).
- **Settings…**: opens the Settings window. **Quit** quits the app (the supervisor keeps running). Unsaved Settings edits, even a value you're still typing, are saved first.

### Supervisor offline banner
When the app can't reach the background supervisor, the dropdown shows a banner at the top (from `ccs daemon status`):
- **Install daemon**: the daemon was never installed (`ccs daemon install`).
- **Start daemon**: the daemon is installed but stopped, or loaded but not responding (`ccs daemon start`).
- **ccs not found at <path>**: the app can't find the `ccs` command. Set its path in Settings (`ccs_path`, default `~/.local/bin/ccs`).

The app also stays offline, and logs why, if the state folder or `daemon.sock` isn't owned by you or the folder is writable by other users.

While it is offline, usage data goes stale, sessions aren't supervised, and statuslines show `⚠ supervisor offline`. The app reconnects by itself (after 1, 2, 5, 10, then every 30 s) as soon as the supervisor is back.

## Warm-up triggers
The app forwards two [warm-up](08-warm-up.md) triggers to the supervisor, which decides per profile whether to act:
- **App start**: once per launch, as soon as the app is connected to the supervisor.
- **Unlock / wake**: when the screen is unlocked or the Mac wakes, at most once per minute.

Both need the supervisor to be running; when it isn't, the trigger is skipped.

## Settings window
Standard macOS settings tabs in the toolbar; the window reopens on the tab you used last.

| Tab | Contents |
|-----|----------|
| General | The app icon, name and versions; **Launch CC Supervisor at login**; **Menu bar** label mode; **Display**: color thresholds and time format (read-only) |
| Profiles | Profile list (sign-in dot, ⚠ for invalid settings, **+** / **−** below it). The selected profile shows a header (emoji, name, `ccs --flag`, sign-in state) and four pages: **General** (name, emoji, launcher flag, config dir, default profile, Claude account sign-in/out), **Limits** (supervisor on/off, resume prompt, warn/pause grid, Fable warn-only, Spill into credits), **Warm-up** (model, prompt, triggers, active hours, cooldown, schedule, next/last run), **Statusline** (preview, apply/revert). See [Profiles & sign-in](02-profiles-and-sign-in.md). |
| Notifications | One toggle per kind ([Notifications](09-notifications.md)), **Send Test Notification**, and a shortcut to System Settings › Notifications |
| About | The app icon and versions, what every feature does, and credits |
| Advanced | **ccs** path (`ccs_path`, **Browse…**, empty = `~/.local/bin/ccs`) and version; **Daemon** status with **Install / Start / Stop / Restart** and **Show Logs…**; **Diagnostics**: **Run Diagnostics** (`ccs doctor`), problems first, passing checks collapsed |

Changes are saved about half a second after you stop typing and are then checked with `ccs config validate`. Invalid values are marked in red and a banner appears; they stay in the file until you fix them, and the supervisor keeps using the last valid config meanwhile. Settings that change elsewhere while the window is open show up within about 2 seconds; if you and another program changed the same setting, the app asks which to keep.

## Launch at login
Settings → General → **Launch CC Supervisor at login**. It stays off until you turn it on (or run `make install`, which turns it on). Keep it on: without the app running, notifications fall back to plain script notifications, and app-start and unlock/wake warm-ups don't happen.

For scripts: `"CC Supervisor.app/Contents/MacOS/CC Supervisor" --register-login-item` (or `--unregister-login-item`) sets it and exits.

## Links (URL scheme)
You can open these from scripts, a browser, or `open`:

| Link | Does |
|------|------|
| `ccsupervisor://profile/<id>` | Opens Settings on that profile (clicking a widget does this) |
| `ccsupervisor://signin/<id>` | Asks for confirmation, then starts sign-in for that profile (Cancel does nothing) |
| `ccsupervisor://refresh` | Fetches usage now |

```sh
open "ccsupervisor://profile/work"
```
Profile ids are the lowercase slugs from your config (`work`, `personal`). Unknown links are ignored.
