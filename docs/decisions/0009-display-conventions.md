# ADR-0009: Display conventions: colors, times, statusline format

- Status: accepted
- Date: 2026-09-24
- Source: user decisions (colors, emoji icons, statusline layout "Spec + limits on warn") + planner defaults (time formats)

## Decision

### Colors (global, `display.colors`)
- `percent < 50` → **green**, `50–79` → **yellow**, `≥ 80` → **red**.
- The same rule applies to every bar: session, weekly, model-scoped, extra usage.
- Python computes `level` (`green|yellow|red`). Swift maps levels to system colors (`.green`, `.yellow`, `.red`). Terminal ANSI: green `32`, yellow `33`, red `31`, separators `90`.

### Icons
- Emoji only, per profile (`profile.emoji`), used identically in the widget, the menu bar, and the statusline.

### Times (24h, local time zone)
- **Absolute reset:**
  - same day: `20:00`
  - within 6 days: `Sat 08:00`
  - later: `26 Sep 08:00`
- **Relative:** `in 2h 13m`, `in 42m`, `in 3d 4h`, `in <1m`, `now`. Round down to minutes; drop zero minor units (`in 2h`).
- **Combined:** `20:00 (in 2h 13m)`.
- Test vectors live in `schema/fixtures/time_format.json`. **Both** the Python and Swift test suites must pass them.

### Statusline
- **Base format** (user spec): `{emoji} {name} ~ {folder} ~ {model} / {effort} ~ {session}`
  - `folder` = basename of `workspace.current_dir`
  - `model` = `model.display_name`
  - `effort` = `effort.level`. When it is absent, the whole ` / {effort}` part is omitted.
  - separator ` ~ ` in gray
- **Session segment states:**
  - normal: `45% ▓▓▓▓▓░░░░░ 20:00 (in 2h 13m)`
  - warn: `⚠ 84% ▓▓▓▓▓▓▓▓░░ 20:00 (in 42m) · limit approaching`
  - paused: `⏸ 90% ▓▓▓▓▓▓▓▓▓░ paused → resumes 20:00 (in 42m)`. When the pause is caused by a weekly or model-scoped hold, it shows that hold's resume time.
  - overridden: `⚠ 91% ▓▓▓▓▓▓▓▓▓░ 20:00 (in 42m) · override`
  - manual hold (no `resume_at`): `⏸ 45% ▓▓▓▓▓░░░░░ paused (manual)`
  - credit-cap hold only (spill mode, no `resume_at`): `⏸ 45% ▓▓▓▓▓░░░░░ paused (credits)`
  - no data: `?% ░░░░░░░░░░`
- **Extra segments**, appended in this order and shown only when ≥ warn (or when they are the active hold):
  - weekly: ` ~ W ⚠ 86% Sat 08:00`
  - model-scoped: ` ~ Fable ⚠ 82% Sat 08:00`
  - extra usage: ` ~ € 3.20/10.00`. Shown only when spill is active **and** one of these holds: extra ≥ warn, an extra-usage hold is active, or session ≥ 100%.
- **Supervisor offline:** append ` ~ ⚠ supervisor offline`. Detection is file-only: `usage/<profile>.json` is missing or its mtime is older than 5 min (ADR-0005).
- The bar has 10 cells, `▓` filled and `░` empty, with `filled = round(percent / 10)`. The percent and the bar are colored by level.
- **Data precedence:** stdin `rate_limits` (freshest) > `usage/<profile>.json`. Supervision state comes from `sessions/$CCS_WRAPPER_ID.json`.
- **Unsupervised sessions:** plain `claude` in a config dir where `apply` was run has no `CCS_WRAPPER_ID`. It renders usage, warnings, and extra segments, but never ⏸ or override. `⚠ supervisor offline` appears only when the daemon is down.

### Widgets and menu bar
- Same labels (`Session`, `Weekly`, `<model name>`, `Extra usage`), the same level colors, and the same absolute and relative formats. Relative strings are precomputed per timeline entry.
- Compact combined form for widgets and menu bar rows: `20:00 · in 2h 13m`. The statusline keeps `20:00 (in 2h 13m)`.
