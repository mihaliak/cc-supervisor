# Menu bar app

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

`CC Supervisor.app` runs in the menu bar only (no Dock icon). It powers the widgets, shows notifications, forwards app-start and unlock/wake events for [warm-ups](08-warm-up.md), and holds the Settings window. All data and actions come from `ccs`. The app itself makes no decisions.

## Menu bar label
Set in Settings → General (`display.menu_bar`):

| Mode | Looks like |
|------|------------|
| Emoji + percent (default) | `💼 45%  🏠 12%`: each profile's session usage, colored green/yellow/red. `?%` means no session data yet (e.g. sign-in required). |
| Icon only | a single gauge icon tinted by the worst level across all profiles |

The label is drawn as a small colored image, so the colors survive in both light and dark menu bars.

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
- **Settings…**: opens the Settings window. **Quit** quits the app (the supervisor keeps running).

### Supervisor offline banner
When the app can't reach the background supervisor, the dropdown shows a banner at the top (from `ccs daemon status`):
- **Install daemon**: the daemon was never installed (`ccs daemon install`).
- **Start daemon**: the daemon is installed but stopped, or loaded but not responding (`ccs daemon start`).
- **ccs not found at <path>**: the app can't find the `ccs` command. Set its path in Settings (`ccs_path`, default `~/.local/bin/ccs`).

While it is offline, usage data goes stale, sessions aren't supervised, and statuslines show `⚠ supervisor offline`. The app reconnects by itself (after 1, 2, 5, 10, then every 30 s) as soon as the supervisor is back.

## Warm-up triggers
The app forwards two [warm-up](08-warm-up.md) triggers to the supervisor, which decides per profile whether to act:
- **App start**: once per launch, as soon as the app is connected to the supervisor.
- **Unlock / wake**: when the screen is unlocked or the Mac wakes, at most once per minute.

Both need the supervisor to be running; when it isn't, the trigger is skipped.

## Settings window
| Tab | Contents |
|-----|----------|
| General | Launch at login, menu bar label mode, notification toggles ([Notifications](09-notifications.md)), path to `ccs`, daemon status with Start / Stop / Restart |
| Profiles | Profile list plus the editor: identity, sign-in, limits, supervisor, statusline, warm-up ([Profiles & sign-in](02-profiles-and-sign-in.md)) |

Every change is validated immediately. Invalid values are shown inline and aren't applied.

## Launch at login
Settings → General → **Launch at login**. It stays off until you turn it on (or run `make install`, which turns it on). Keep it on: without the app running, notifications fall back to plain script notifications, and app-start and unlock/wake warm-ups don't happen.

For scripts: `"CC Supervisor.app/Contents/MacOS/CC Supervisor" --register-login-item` (or `--unregister-login-item`) sets it and exits.

## Links (URL scheme)
You can open these from scripts, a browser, or `open`:

| Link | Does |
|------|------|
| `ccsupervisor://profile/<id>` | Opens Settings on that profile (clicking a widget does this) |
| `ccsupervisor://signin/<id>` | Starts sign-in for that profile |
| `ccsupervisor://refresh` | Fetches usage now |

```sh
open "ccsupervisor://profile/work"
```
Profile ids are the lowercase slugs from your config (`work`, `personal`). Unknown links are ignored.
