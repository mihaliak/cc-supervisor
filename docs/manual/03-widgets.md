# Widgets

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

Each widget shows **one profile**. Add as many as you like, for example one per profile, or the same profile in two sizes.

## Adding a widget
1. Right-click the desktop → **Edit Widgets…**
2. Search **CC Supervisor** and pick **Small**, **Medium**, or **Large**. Drag it to the desktop or Notification Center.
3. Right-click the widget → **Edit Widget**:

| Option | Sizes | Effect |
|--------|-------|--------|
| Profile | all | Which profile to show (the list comes from your profiles) |
| Show weekly | medium, large | Weekly row on or off |
| Show model-scoped | medium, large | Fable (or other model) row on or off |
| Show extra usage | medium, large | Extra-usage row on or off |

Everything else (name, emoji, config dir, sign-in, thresholds) is a **profile** setting, changed in the app's Settings. Click a widget to open Settings on its profile.

## Sizes
Mockups use example numbers. Bars are native, colored green, yellow, or red.

**Small**: session first, weekly compact
```
┌───────────────────────┐
│ 💼 Work               │
│                       │
│ Session          45%  │
│ █████████░░░░░░░░░░░  │
│ 20:00 · in 2h 13m     │
│                       │
│ Weekly 50% · Sat 08:00│
└───────────────────────┘
```

**Medium**: every limit with its bar and reset
```
┌──────────────────────────────────────────────┐
│ 💼 Work                                      │
│ Session       45%  █████████░░░░░░░░░░░       │
│               20:00 · in 2h 13m              │
│ Weekly        50%  ██████████░░░░░░░░░░       │
│               Sat 08:00 · in 1d 13h          │
│ Fable          4%  █░░░░░░░░░░░░░░░░░░░       │
│               Sat 08:00 · in 1d 13h          │
│ Extra usage    0%  €0.00 / €10.00 · off       │
└──────────────────────────────────────────────┘
```

**Large**: medium plus supervisor state
```
┌──────────────────────────────────────────────┐
│ 💼 Work                          ⏸ Paused    │
│ Session       90%  ██████████████████░░       │
│               20:00 · in 42m                 │
│ Weekly        61%  ████████████░░░░░░░░       │
│               Sat 08:00 · in 1d 13h          │
│ Fable         12%  ██░░░░░░░░░░░░░░░░░░       │
│               Sat 08:00 · in 1d 13h          │
│ Extra usage    0%  €0.00 / €10.00 · off       │
│ ──────────────────────────────────────────── │
│ ccs sessions   3 active · 2 paused           │
│ Next warm-up   20:00 (auto-chain)            │
│ Updated 19:18                                │
└──────────────────────────────────────────────┘
```

Row details:
- **Session**: 5-hour window.
- **Weekly**: 7-day, all models.
- **Model-scoped**: labeled with the model's name (for example **Fable**). Shown only when Claude reports such a limit for the account.
- **Extra usage**: percent of the monthly credit cap, used/limit in your account currency, and `off` when credits are disabled.
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

The limits are set in the config (`display.colors`, see [Configuration](10-configuration-reference.md)).

## States
| State | What you see | What to do |
|-------|--------------|------------|
| Normal | Rows as above | – |
| Paused | **⏸ Paused** badge. The session row shows when work resumes. | Nothing; it resumes automatically ([Limits & supervisor](07-limits-and-supervisor.md)) |
| Sign in required | "Sign in required" instead of rows | Click the widget, then **Sign in** |
| Supervisor offline | Last values dimmed, footer "Supervisor offline" | The supervisor hasn't written anything for more than 5 minutes. Start it from the menu bar banner, or run `ccs daemon start` / `ccs doctor`. |
| Stale | Rows greyed, with "Updated HH:MM" | The supervisor is running but hasn't had fresh usage for more than 10 minutes (e.g. the Mac is offline). Run `ccs doctor` if it persists. |
| No data yet | Placeholder rows | Wait for the first poll (about 1 minute after sign-in) |
| Profile missing | "Choose a profile" | The profile was removed. Pick another in **Edit Widget**. |

## How fresh is the data?
- The supervisor checks usage about every minute (every 20 s near limits), and instantly while a `ccs` session is working.
- The app asks macOS to redraw widgets **at most once a minute**, and immediately for important changes (paused, resumed, signed in).
- **macOS may throttle widget refreshes further.** For a truly live view, use the [menu bar](04-menu-bar-app.md), which updates in real time.
