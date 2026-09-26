# Configuration reference

> Status: shipped (2026-09-24).

All settings live in one JSON file, `~/.config/ccs/config.json` (or `$XDG_CONFIG_HOME/ccs/config.json`). It never contains passwords or tokens.

Ways to edit it:
- the app: Settings (recommended)
- `ccs profile set …` / `ccs profile add …` / `ccs config set …`
- by hand, then run `ccs config validate`

### Editable in the app vs. CLI only
| Where | Keys |
|-------|------|
| Settings → General / Notifications / Advanced | `display.menu_bar` / `notifications.*` / `ccs_path` |
| Settings → Profiles | `default_profile` (**Default profile** toggle), every profile key except `id`: `flag`, `name`, `emoji`, `config_dir`, `limits.*`, `supervisor.*`, `statusline.enabled`, `warmup.*` (cooldown 1–120 min in the app) |
| CLI or by hand only | `display.colors.*`, `display.time_format` (only `24h`), `claude_path`, `polling.*`, cooldowns outside 1–120 min. `id` can't be changed at all; `version` and `revision` are managed by the tools. |

The app and `ccs` write the file byte for byte the same way, bump `revision` on every write, and merge concurrent changes to different keys (the app asks when both changed the same key).

The supervisor picks up changes within about 2 seconds. If the file becomes invalid, the last valid config stays in effect and you get a "config invalid" notification.

```sh
ccs config path       # where the file is
ccs config show       # effective config, with defaults filled in
ccs config defaults   # all default values
ccs config validate   # check for errors
```

## Top level
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `version` | int | `1` | File format version. Don't edit. |
| `revision` | int | `0` | Change counter, managed automatically so edits from the app and the CLI never overwrite each other. Don't edit. |
| `default_profile` | string \| null | `"personal"` | Profile `id` used by `ccs` with no profile flag. `null` means no default. When the key is missing, `personal` is the default only if that profile exists. Without a default, plain `ccs` uses the only profile, or asks for a profile flag. |
| `ccs_path` | string \| null | `null` | Path the app uses to run `ccs`. `null` means `~/.local/bin/ccs`. |
| `claude_path` | string \| null | `null` | Path to `claude`. `null` means find it on the `PATH` captured at `ccs daemon install`. |

## `display`
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `display.time_format` | string | `"24h"` | Clock format for reset times (24-hour) |
| `display.colors.yellow_from` | int (%) | `50` | Bars turn yellow at this percent |
| `display.colors.red_from` | int (%) | `80` | Bars turn red at this percent |
| `display.menu_bar` | `"letter_percent"` \| `"icon_only"` | `"letter_percent"` | Menu bar label style ([Menu bar app](04-menu-bar-app.md)) |

## `polling`
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `polling.interval_seconds` | int | `60` | Normal usage check interval, per profile |
| `polling.fast_interval_seconds` | int | `20` | Interval when a limit is high and a `ccs` session is working |
| `polling.fast_when_percent_at_least` | int (%) | `70` | "High" means at or above this percent |
| `polling.idle_interval_seconds` | int | `120` | Interval when the profile has no `ccs` sessions |

## `notifications`
All are booleans, default `true` ([Notifications](09-notifications.md)).

| Key | Covers |
|-----|--------|
| `notifications.limit_warn` | warn thresholds reached |
| `notifications.limit_pause` | sessions paused |
| `notifications.limit_resume` | sessions resumed |
| `notifications.warmup` | warm-up started a window, or failed |
| `notifications.errors` | sign-in required, usage unreadable, config invalid |

## `profiles[]`
Each entry is one profile ([Profiles & sign-in](02-profiles-and-sign-in.md)).

### Identity
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `id` | string | – (required) | Internal ID matching `^[a-z0-9][a-z0-9-]{0,31}$`. Can't be changed after creation. |
| `flag` | string | – (required) | Launcher flag: `work` means `ccs --work` |
| `name` | string | – (required) | Display name |
| `emoji` | string | – (required) | Icon for the widget, menu bar, and statusline |
| `config_dir` | string | – (required) | Claude Code config dir, e.g. `~/.claude-work` |

### `limits`
Thresholds in percent ([Limits & supervisor](07-limits-and-supervisor.md)).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `limits.session.warn` | int | `80` | Warn threshold for the 5-hour window |
| `limits.session.pause` | int | `90` | Pause threshold for the 5-hour window |
| `limits.weekly.warn` | int | `80` | Warn threshold for the weekly limit |
| `limits.weekly.pause` | int | `95` | Pause threshold for the weekly limit |
| `limits.model_scoped.warn` | int | `80` | Warn threshold for model-scoped limits (e.g. Fable) |
| `limits.model_scoped.pause` | int | `95` | Pause threshold (only sessions on that model), or the second warning when `warn_only` is on |
| `limits.model_scoped.warn_only` | bool | `false` | `true`: never pause for model-scoped limits, only warn |
| `limits.extra_usage.spill` | bool | `false` | `true`: while credits are enabled, skip session and weekly pauses and pause on credit usage instead |
| `limits.extra_usage.warn` | int | `80` | Warn at this percent of the monthly credit cap |
| `limits.extra_usage.pause` | int | `90` | Pause at this percent of the cap (only with `spill: true`) |

### `supervisor`
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `supervisor.enabled` | bool | `true` | Supervise this profile's `ccs` sessions (pause and resume). `false`: warnings and warm-ups still work; active pauses are lifted and `ccs pause` is refused |
| `supervisor.resume_prompt` | string | `"The usage limit window has reset. Continue exactly where you left off."` | Typed into sessions that were interrupted mid-work (busy when paused), when they resume. Idle or overridden sessions get nothing. If the pause stopped subagents or workflows, a note asking Claude to start them again is appended |

### `statusline`
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `statusline.enabled` | bool | `true` | Show the CC Supervisor statusline in `ccs` sessions ([Statusline](06-statusline.md)). When `false`, `ccs statusline apply` refuses. The script's name, emoji, limits and `display.colors` are baked in at generation; the supervisor refreshes existing scripts when they change. |

### `warmup`
([Warm-up](08-warm-up.md))

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `warmup.enabled` | bool | `true` | Master switch for this profile's warm-ups |
| `warmup.model` | string | `"haiku"` | Model for the warm-up request |
| `warmup.prompt` | string | `"Reply with just: ok"` | Warm-up prompt |
| `warmup.triggers.schedule` | array | `[]` | Entries of `{ "time": "HH:MM", "weekdays": ["mon", …] }` in local time; each occurrence runs once. An empty list turns the schedule trigger off |
| `warmup.triggers.app_start` | bool | `true` | Warm up when the app launches |
| `warmup.triggers.unlock_wake` | bool | `true` | Warm up on screen unlock or wake |
| `warmup.triggers.auto_chain` | bool | `true` | Start the next window when one resets, during active hours |
| `warmup.active_hours.start` | `"HH:MM"` | `"07:00"` | Start of active hours (included). Limits auto-chain and missed-schedule catch-ups |
| `warmup.active_hours.end` | `"HH:MM"` | `"23:00"` | End of active hours (excluded). May be earlier than `start` to cross midnight; equal to `start` means all day |
| `warmup.cooldown_minutes` | int | `10` | Minimum gap between warm-up attempts for this profile (skips don't count) |

### Seeded profiles
| id | flag | name | emoji | config_dir |
|----|------|------|-------|------------|
| `personal` | `personal` | Personal | 🏠 | `~/.claude` |
| `work` | `work` | Work | 💼 | `~/.claude-work` |

## Validation rules
- `version` and `profiles` must be present.
- `version` is `1`. A file with a higher version is rejected with a message to upgrade `ccs`. `revision` is a whole number ≥ 0.
- `default_profile` is `null` or the `id` of an existing profile (checked only while at least one profile exists). Only the value in the file is checked: a missing key is fine.
- Numbers must be finite everywhere in the file, unknown keys included: `Infinity`, `NaN` or an overflowing `1e999` is an error (they aren't JSON, and the app can't read such a file).
- `ccs_path` and `claude_path` are `null` or an absolute path (or one starting with `~`). A relative path would depend on the folder you run `ccs` in.
- Profile `id` and `flag`:
  - each must be unique
  - both use lowercase letters, digits, and `-`, start with a letter or digit, and are at most 32 characters
- `flag` must not be reserved:
  - ccs options: `help`, `version`, `profile`, `force`, `no-supervise`, `json`
  - any `claude` long option (e.g. `model`, `resume`, `continue`, `print`, `effort`)
- `config_dir` must be unique across profiles (after `~` expansion), and absolute or starting with `~`.
- `emoji` is required, at most 8 characters (code points).
- `name` and `emoji` can't be blank. `name`, `emoji`, `config_dir` and the paths can't contain control characters (such as a newline).
- Thresholds are whole numbers from 1 to 100, and each `warn` must be lower than its `pause`.
- Colors: `0 < yellow_from < red_from ≤ 100`.
- `name`, `warmup.model`, `warmup.prompt`, and `supervisor.resume_prompt` are non-empty strings; toggles are `true`/`false`.
- `warmup.prompt` and `supervisor.resume_prompt` can contain newlines and tabs but no other control characters. `warmup.prompt` can't start with `-` (`claude` would read it as an option).
- `warmup.model` is a model name or id: letters, digits and `._:@/[]-`, for example `haiku`, `opus[1m]`, `claude-opus-5-5`.
- Times are `HH:MM` (24h). Each schedule entry needs at least one weekday; weekdays are `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`, with no duplicates.
- `cooldown_minutes` is 0–1440. Polling intervals are 5–240 seconds, so the supervisor's files are always rewritten well within the 5-minute offline limit.
- Every pattern (ids, flags, `HH:MM`) must match the whole value. A trailing newline or non-ASCII digits make it invalid.
- `display.menu_bar` is `letter_percent` or `icon_only` (the older `emoji_percent` is accepted and means `letter_percent`). `display.time_format` is `24h`.
- Unknown keys are kept as they are, so newer settings survive older tools.
- The file is written with sorted keys, 2-space indentation, and literal emoji, by both the app and `ccs`. `ccs` never adds defaults to the file: keys you leave out keep following the defaults.
- A JSON Schema of the file is in the repo at `schema/config.schema.json`. `ccs config validate` also checks the cross-field rules above (unique ids, warn < pause, reserved flags).

## Example
```json
{
  "version": 1,
  "revision": 7,
  "default_profile": "work",
  "display": { "time_format": "24h", "colors": { "yellow_from": 50, "red_from": 80 }, "menu_bar": "letter_percent" },
  "profiles": [
    {
      "id": "work", "flag": "work", "name": "Work", "emoji": "💼", "config_dir": "~/.claude-work",
      "limits": { "weekly": { "warn": 80, "pause": 97 }, "model_scoped": { "warn_only": true } },
      "warmup": { "triggers": { "schedule": [ { "time": "06:00", "weekdays": ["mon","tue","wed","thu","fri"] } ] } }
    }
  ]
}
```
Missing keys take their defaults.

## File locations
| What | Where |
|------|-------|
| Config | `~/.config/ccs/config.json` |
| Runtime data | `~/.local/state/ccs/` |
| `ccs` command | `~/.local/bin/ccs` |
| App | `~/Applications/CC Supervisor.app` |
| Background supervisor | `~/Library/LaunchAgents/local.ccsupervisor.daemon.plist` (label `local.ccsupervisor.daemon`) |
| Statusline script | `<config dir>/ccs-statusline.py` |
| `settings.json` backup (from statusline apply) | `<config dir>/settings.json.ccs-backup-<YYYYmmddHHMMSS>` |
| URL scheme | `ccsupervisor://` |

## Runtime data (for power users)
`~/.local/state/ccs/`. Safe to read; don't edit while the supervisor runs.

| Path | Contents |
|------|----------|
| `usage/<profile>.json` | latest usage per profile |
| `sessions/<id>.json` | one file per supervised `ccs` session, incl. its pause state (`schema/session-record.schema.json`) |
| `supervisor/<profile>.json` | active pauses ("holds"), which limit windows already warned or paused, and the last 200 supervisor actions; survives restarts (`schema/supervisor-state.schema.json`) |
| `warmup/<profile>.json` | last attempt, last skip, next planned run, the last 20 attempts, and which scheduled times already ran |
| `statusline/<profile>.json` | what `statusline apply` changed, used by `revert` |
| `widget/snapshot.json` | what the widgets and menu bar display |
| `live/` | live usage reported by statuslines |
| `events.jsonl` | event history (what `ccs events` shows) |
| `events.seen.json` | notification dedupe memory |
| `logs/daemon.log` | supervisor log (what `ccs daemon logs` shows) |
| `daemon.sock`, `daemon.lock` | supervisor socket and lock |
