# CC Supervisor manual

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

CC Supervisor helps you keep track of, and get the most out of, Claude Code subscription limits when you use **several Claude Code accounts or config dirs** (for example personal and work). It has three parts:
- **Desktop widgets and a menu bar app** (native macOS) show each profile's session, weekly, model-scoped (for example Fable), and extra-usage limits. Each limit has a colored bar, its reset time, and a relative countdown.
- **`ccs`** is a command-line launcher. `ccs --work` opens the normal interactive Claude Code in your terminal with the right config dir and a usage statusline, while a background supervisor watches the limits.
- **The supervisor** warns you when a limit is approaching, pauses your `ccs` sessions before the limit is used up, and resumes them automatically when the window resets. It can also start a session window early ("warm-up") so you get more windows per day.

## How the pieces fit
```
 Desktop widgets ──────┐ read
 Menu bar app ─────────┤ read + `ccs … --json` + live events
                       ▼
             ~/.local/state/ccs/   (usage, sessions, widget snapshot, events)
                       ▲
                       │ writes
             ccs daemon (background, launchd)
                       │ asks Claude Code for usage · lists sessions · runs warm-ups
                       ▼
 terminal: ccs --work ──► claude (classic TUI) ──► statusline script
           (supervised)         ▲                     reads state, reports live usage
                                └── pause / resume keystrokes from the supervisor
```
- The Swift app and widgets only display data and forward clicks. All logic runs in the Python `ccs` tool.
- CC Supervisor stores **no passwords or tokens**. Each profile uses Claude Code's own sign-in.

## Features
- One widget per profile, in small, medium, or large size, configured with right-click → Edit Widget.
- Menu bar summary of every profile, plus quick actions.
- Per-profile settings: name, emoji, Claude config dir, sign-in, thresholds, warm-ups, statusline.
- `ccs --<profile>` launcher: identical Claude Code experience, supervised.
- Statusline: `{emoji} {name} ~ {folder} ~ {model} / {effort} ~ {usage % + bar + reset}`.
- Warnings at 80%, auto-pause at 90% (session) or 95% (weekly), and auto-resume at reset. All thresholds are configurable.
- Model-scoped (Fable) and extra-usage (paid credits) handling, each with its own per-profile toggle.
- Warm-ups on a schedule, at app start, at unlock or wake, and automatically when a window resets.
- Native macOS notifications.

## Screenshots
<!-- screenshot: widgets (small, medium, large) on the desktop -->
<!-- screenshot: menu bar dropdown -->
<!-- screenshot: statusline in a ccs session -->

## Quick start (5 minutes)
1. **Install.** Check the [prerequisites](01-installation.md), then run this in the repo:
   ```sh
   make install
   ```
   It checks the prerequisites, installs `ccs`, the app and the background supervisor, and ends with `ccs doctor`. It doesn't change anything in your Claude config dirs.
2. **Profiles.** Two are created for you: `personal` (`~/.claude`, 🏠) and `work` (`~/.claude-work`, 💼). Adjust them in menu bar → **Settings… → Profiles**, or see [Profiles](02-profiles-and-sign-in.md).
3. **Sign in.** For each profile, click **Sign in**. That's a claude.ai sign-in in your browser; it is Claude Code's own login for that config dir.
4. **Add widgets.** Right-click the desktop → **Edit Widgets** → search "CC Supervisor". Then right-click a widget → **Edit Widget** and pick a profile.
5. **Work.**
   ```sh
   ccs --work          # classic Claude Code, work account, supervised
   ccs --personal -c   # continue the last personal conversation
   ```
6. **Something off?** `ccs doctor` checks everything and tells you how to fix it.

## Pages
| Page | What's inside |
|------|---------------|
| [01 Installation](01-installation.md) | Prerequisites, install, first launch, upgrade, uninstall |
| [02 Profiles & sign-in](02-profiles-and-sign-in.md) | Creating and editing profiles, flags, signing in and out |
| [03 Widgets](03-widgets.md) | Sizes, contents, configuration, states, refresh |
| [04 Menu bar app](04-menu-bar-app.md) | Label, dropdown, actions, Settings window, links |
| [05 ccs CLI](05-ccs-cli.md) | Full command reference |
| [06 Statusline](06-statusline.md) | Format, states, generate/apply/revert |
| [07 Limits & supervisor](07-limits-and-supervisor.md) | Warn, pause, resume, override, weekly, Fable, extra usage |
| [08 Warm-up](08-warm-up.md) | Starting session windows early |
| [09 Notifications](09-notifications.md) | What you get notified about, and toggles |
| [10 Configuration reference](10-configuration-reference.md) | Every `config.json` key, file locations |
| [11 Troubleshooting](11-troubleshooting.md) | `ccs doctor`, logs, common problems |

## Glossary
- **Profile**: one Claude Code identity managed by CC Supervisor. It has a name, an emoji, a launcher flag (`--work`), and a config dir.
- **Config dir**: the folder Claude Code uses for settings, history, and login (`CLAUDE_CONFIG_DIR`). `~/.claude` is the default.
- **Session window (5h)**: Claude's rolling 5-hour usage limit. It starts with your first usage and resets 5 hours later.
- **Weekly limit**: the 7-day usage limit across all models.
- **Model-scoped limit**: a separate weekly limit for a specific model, for example **Fable**.
- **Extra usage**: paid usage credits that continue your work after you reach the plan limits, capped by a monthly limit.
- **Warn**: a threshold at which you get a statusline warning and a notification (default 80%).
- **Pause**: a threshold at which supervised sessions are stopped (default 90% session, 95% weekly and model-scoped).
- **Resume**: automatically continuing paused sessions once the limit window has reset.
- **Hold**: an active reason to keep sessions paused (session, weekly, model-scoped, or extra usage). Sessions resume only when all their holds clear.
- **Override**: you typed and submitted a prompt in a paused session. From then on it runs freely for the rest of that window and gets no automatic resume prompt.
- **Warm-up**: a tiny, cheap Claude request sent only to start a new 5-hour window early.
- **Supervised session**: an interactive Claude Code session started with `ccs`. Only these are paused and resumed. Background agents and plain `claude` sessions are only counted.
