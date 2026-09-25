# Widgets

> Status: shipped (2026-09-24). The GUI parts (menu bar, Settings, widgets, native notifications) haven't had a human check yet.

Each widget shows **one profile**. Add as many as you like, for example one per profile, or the same profile in two sizes.

## Adding a widget
1. Right-click the desktop → **Edit Widgets…**
2. Search **CC Supervisor** (the widget is called **Claude usage**) and pick **Small**, **Medium**, or **Large**. Drag it to the desktop or Notification Center.
3. Right-click the widget → **Edit Widget**:

| Option | Sizes | Effect |
|--------|-------|--------|
| Profile | all | Which profile to show. The list comes from your profiles; a new widget starts with the first one. Widgets added before 2026-09-25 need their profile picked again once. |
| Small widget style | small | **Progress bar** (default): big percent, a bar and the reset time. **Gauge**: a half-circle gauge like the app icon, colored green/yellow/red by the same thresholds, with its needle at the session %, the percent under it and the reset time. |
| Show weekly | medium, large | Weekly row on or off |
| Show model limits | medium, large | Fable (or other model) rows on or off |
| Show extra usage | medium, large | Extra-usage row on or off |

Edit Widget only lists the options that apply to the widget's size. The small size always ends with a one-line weekly summary.

Everything else (name, emoji, config dir, sign-in, thresholds) is a **profile** setting, changed in the app's Settings. Click a widget to open Settings on its profile.

## Sizes
Mockups use example numbers. Bars are native, colored green, yellow, or red.

**Small**: the session, big, plus a weekly line
```
┌───────────────────────┐
│ 💼 Work             ⏸ │
│                       │
│ 45%                   │
│ █████████░░░░░░░░░░░  │
│ 20:00 · in 2h 13m     │
│                       │
│ W 50% · Sat 08:00     │
└───────────────────────┘
```

**Medium**: one line per limit, up to four
```
┌──────────────────────────────────────────────┐
│ 💼 Work                          ⏸ Paused    │
│ Session      45%  █████░░░  20:00 · in 2h 13m │
│ Weekly       50%  █████░░░  Sat 08:00 · in 1d │
│ Fable         4%  ░░░░░░░░  Sat 08:00 · in 1d │
│ Extra usage  32%  ███░░░░░  €3.20 / €10.00    │
└──────────────────────────────────────────────┘
```
If more limits exist than fit, the medium size drops rows in this order: extra usage, then model limits, then weekly. The session row always stays.

**Large**: the limits stacked, plus supervisor state
```
┌──────────────────────────────────────────────┐
│ 💼 Work                          ⏸ Paused    │
│ resumes 20:00 · in 42m                       │
│ Session                                  90% │
│ ██████████████████░░                         │
│ 20:00 · in 42m                               │
│ Weekly                                   61% │
│ ████████████░░░░░░░░                         │
│ Sat 08:00 · in 1d 13h                        │
│ Fable                                    12% │
│ ██░░░░░░░░░░░░░░░░░░                         │
│ Sat 08:00 · in 1d 13h                        │
│ ──────────────────────────────────────────── │
│ 3 ccs sessions · 2 paused · 1 other          │
│ Next warm-up Fri 06:00 · in 12h 13m          │
│ Updated 1m ago                               │
└──────────────────────────────────────────────┘
```
- The large size shows up to six limit rows.
- The `resumes …` line appears only while paused.
- "ccs sessions" are sessions started with `ccs`; "other" counts background agents and plain `claude` sessions. "paused" is left out when nothing is paused.
- "Next warm-up" appears only when one is scheduled.

Row details:
- **Session**: 5-hour window.
- **Weekly**: 7-day, all models.
- **Model limits**: labeled with the model's name (for example **Fable**). Shown only when Claude reports such a limit for the account.
- **Extra usage**: percent of the monthly credit cap, with used / limit in your account currency (for example `€3.20 / €10.00`).
- **Reset**: 24h local time.
  - same day: `20:00`
  - within 6 days: `Sat 08:00`
  - later: `26 Sep 08:00`
- **Countdown**: `in 2h 13m`, `in 42m`, `in 3d 4h`, `in <1m`, `now`. It updates every minute without needing new data.

## Colors
Same everywhere (widgets, menu bar, statusline):

| Usage | Color |
|-------|-------|
| below 50% | green |
| 50–79% | yellow |
| 80% and above | red |

The limits are set in the config (`display.colors`, see [Configuration](10-configuration-reference.md)). The widget only shows the color the supervisor computed; it doesn't apply thresholds itself.

## States
| State | What you see | What to do |
|-------|--------------|------------|
| Normal | Rows as above | – |
| Paused | **⏸ Paused** badge; the large size also shows `resumes 20:00 · in 42m` | Nothing; it resumes automatically ([Limits & supervisor](07-limits-and-supervisor.md)) |
| Sign in required | "Sign in required" instead of rows | Click the widget: Settings opens on the profile, then click **Sign in** |
| Supervisor offline | Last values dimmed, footer "Supervisor offline" (or "No usage data yet" if there's nothing to show) | When the widget last loaded data, the supervisor hadn't written anything for more than 5 minutes. Start it from the menu bar banner, or run `ccs daemon start` / `ccs doctor`. |
| Stale | Values dimmed, footer "updated 14m ago" | The data shown is more than 10 minutes old: either the supervisor has no fresh usage (e.g. the Mac is offline) or macOS hasn't refreshed the widget for a while. Run `ccs doctor` if it persists. |
| No data | "No usage data yet", "Usage unavailable" or "No plan limits for this account" | Wait for the first poll (about 1 minute after sign-in); for "Usage unavailable" run `ccs doctor` |
| Not set up | "Choose a profile" · "Right-click → Edit Widget" | No profile is selected, or the profile was removed. Pick one in **Edit Widget**. |

## How fresh is the data?
- The supervisor checks usage about every minute (every 20 s near limits), and instantly while a `ccs` session is working.
- The app asks macOS to redraw widgets **at most once a minute**, and immediately for important changes (paused, resumed, signed in).
- Each redraw carries an hour of per-minute entries, so countdowns keep ticking even if macOS delays the next redraw.
- **macOS may throttle widget refreshes further.** A widget that hasn't been refreshed for more than 10 minutes shows its data as stale. For a truly live view, use the [menu bar](04-menu-bar-app.md), which updates in real time.
