# Menu bar app

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

`CC Supervisor.app` runs in the menu bar only (no Dock icon). It powers the widgets, shows notifications, forwards app-start and unlock/wake events for [warm-ups](08-warm-up.md), and holds the Settings window. All data and actions come from `ccs`. The app itself makes no decisions.

## Menu bar label
Set in Settings → General (`display.menu_bar`):

| Mode | Looks like |
|------|------------|
| Emoji + percent (default) | `💼 45%  🏠 12%`: each profile's session usage, colored green/yellow/red |
| Icon only | a single gauge icon tinted by the worst level across all profiles |

## Dropdown
```
┌────────────────────────────────────────────┐
│ 💼 Work                         ⏸ Paused   │
│   Session     90% ██████████████████░░      │
│               20:00 · in 42m                │
│   Weekly      61% Sat 08:00 · in 1d 13h     │
│   Fable       12% Sat 08:00 · in 1d 13h     │
│   Extra usage off (€0.00 / €10.00)          │
│   ccs: 3 active · 2 paused · other: 1       │
│   Next warm-up 20:00                        │
│ ────────────────────────────────────────── │
│ 🏠 Personal                                 │
│   Session      1% 20:00 · in 3h 52m         │
│   …                                        │
│ ────────────────────────────────────────── │
│ Refresh                                     │
│ Warm up now                        ▸        │
│ Pause / Resume profile             ▸        │
│ Settings…                                   │
│ Quit                                        │
└────────────────────────────────────────────┘
```
- **Per-profile card:**
  - Session, weekly, model-scoped (for example Fable), and extra usage, each with percent, bar, and reset time.
  - Supervisor state: how many `ccs` sessions are active or paused, how many **other** sessions exist (background agents and plain `claude`, not supervised), and the next warm-up time.
- **Refresh**: fetch usage for all profiles now.
- **Warm up now ▸ profile**: start a session window immediately (`ccs warmup --profile <id>`). The normal [skip rules](08-warm-up.md#skip-rules) still apply.
- **Pause / Resume profile ▸ profile**: manually pause or resume that profile's `ccs` sessions (`ccs pause|resume --profile <id>`).
- **Settings…**: opens the Settings window.

### Supervisor offline banner
When the background supervisor isn't running, the dropdown shows a banner at the top:
- **Start daemon**: the daemon is installed but stopped (`ccs daemon start`).
- **Install daemon**: the daemon was never installed (`ccs daemon install`).

While it is offline, usage data goes stale, sessions aren't supervised, and statuslines show `⚠ supervisor offline`.

## Settings window
| Tab | Contents |
|-----|----------|
| General | Launch at login, menu bar label mode, notification toggles ([Notifications](09-notifications.md)), path to `ccs`, daemon status with Start / Stop / Restart |
| Profiles | Profile list plus the editor: identity, sign-in, limits, supervisor, statusline, warm-up ([Profiles & sign-in](02-profiles-and-sign-in.md)) |

Every change is validated immediately. Invalid values are shown inline and aren't applied.

## Launch at login
Settings → General → **Launch at login**. Keep it on: without the app running, notifications fall back to plain script notifications, and app-start and unlock/wake warm-ups don't happen.

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
