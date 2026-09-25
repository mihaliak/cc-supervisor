# ADR-0004: Config file & profile model

- Status: accepted; partly superseded by [ADR-0022](0022-review-hardening.md)
- Date: 2026-09-24
- Source: user decisions (config split, thresholds, warm-up, extra usage, model-scoped toggle) + planner defaults (file format, location)

## Context
Profiles are shared by the widgets, the menu bar app, `ccs`, the daemon, and the generated statuslines. The user chose "profile lives in the app, widget only picks it". Python cannot read per-widget WidgetKit settings, so profile data must live in one shared file.

## Decision
- One file: `~/.config/ccs/config.json` (`$XDG_CONFIG_HOME/ccs/config.json` if set). It holds no secrets.
- JSON, not TOML, because both Swift (`Codable`) and Python (`json`) handle it natively. The JSON Schema lives at `schema/config.schema.json`.
- Writers:
  - The Swift Settings UI edits the file directly (it is data).
  - `ccs profile|config` commands edit it from the CLI.
  - Both write atomically and use optimistic concurrency via `revision`: re-read, compare, write `revision+1`. On a mismatch, reload and re-apply the change.
- The daemon watches the file's mtime (2 s) and reloads it. An invalid file keeps the last good config and emits a `config.invalid` event.
- After every Swift write, the app runs `ccs config validate --json` and shows any errors inline.

### Shape (v1)
```jsonc
{
  "version": 1,
  "revision": 0,
  "default_profile": "personal",          // `ccs` with no profile flag
  "ccs_path": null,                       // app: null = ~/.local/bin/ccs
  "claude_path": null,                    // null = resolve `claude` on the PATH captured at daemon install
  "display": {
    "time_format": "24h",
    "colors": { "yellow_from": 50, "red_from": 80 },
    "menu_bar": "letter_percent"          // letter_percent | icon_only (ADR-0018; emoji_percent = legacy alias)
  },
  "polling": {
    "interval_seconds": 60,
    "fast_interval_seconds": 20,          // when any window >= fast_when_percent_at_least and a supervised session is busy
    "fast_when_percent_at_least": 70,
    "idle_interval_seconds": 120          // no supervised sessions running
  },
  "notifications": {
    "limit_warn": true, "limit_pause": true, "limit_resume": true,
    "warmup": true, "errors": true
  },
  "profiles": [
    {
      "id": "work",                       // slug ^[a-z0-9][a-z0-9-]{0,31}$, immutable after creation
      "flag": "work",                     // `ccs --work`; unique; must not collide with reserved names
      "name": "Work",
      "emoji": "💼",
      "config_dir": "~/.claude-work",     // stored as typed; expanded to an absolute path at use
      "limits": {
        "session":      { "warn": 80, "pause": 90 },
        "weekly":       { "warn": 80, "pause": 95 },
        "model_scoped": { "warn": 80, "pause": 95, "warn_only": false },
        "extra_usage":  { "spill": false, "warn": 80, "pause": 90 }
      },
      "supervisor": {
        "enabled": true,
        "resume_prompt": "The usage limit window has reset. Continue exactly where you left off."
      },
      "statusline": { "enabled": true },
      "warmup": {
        "enabled": true,
        "model": "haiku",
        "prompt": "Reply with just: ok",
        "triggers": {
          "schedule": [],                 // e.g. [{ "time": "06:00", "weekdays": ["mon","tue","wed","thu","fri"] }]
          "app_start": true,
          "unlock_wake": true,
          "auto_chain": true
        },
        "active_hours": { "start": "07:00", "end": "23:00" },
        "cooldown_minutes": 10
      }
    }
  ]
}
```

### Validation rules
- `id` and `flag` are unique.
- `flag` must not be a reserved name: ccs's own options (`help`, `version`, `profile`, `force`, `no-supervise`, `json`) or any `claude` long option (static list in code, refreshed by `ccs doctor` from `claude --help`).
- `config_dir` must be unique across profiles and must be absolute or start with `~`.
- Percent thresholds are integers 1–100 with `warn < pause`.
- `time` is `HH:MM`, 24h. Weekdays are `mon`…`sun`.

### Seed (first run and migration, P14)
- `personal` → `~/.claude`, 🏠, default profile.
- `work` → `~/.claude-work`, 💼.

## Consequences
- Adding a config field means changing the schema, then Python `config.py` models and defaults, then Swift `Codable` models, then `docs/manual/10-configuration-reference.md`.
- Unknown fields are preserved on write by both sides (forward compatibility).

## Rules for implementers
- Never write partial files. Always use temp file + `os.replace` / `FileManager.replaceItemAt`.
- Serialization is identical on both sides so they don't churn each other's writes: sorted keys, 2-space indent, UTF-8 without escaping (emoji stay literal), trailing newline.
- Top-level keys are changed via `ccs config set`, profile keys via `ccs profile set` (ADR-0017).
- Missing optional fields fall back to the defaults above, which are defined once in Python (`ccs.config.defaults`) and exported by `ccs config defaults --json` for Swift.
