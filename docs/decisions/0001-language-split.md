# ADR-0001: Language split: Swift = native UI only, Python = everything else

- Status: accepted
- Date: 2026-09-24
- Source: user decision ("widgets needs to be native in swift, everything else in python")

## Context
The product has native macOS surfaces (widgets, menu bar, settings window, notifications) and a lot of logic: usage polling, limit policy, pausing and resuming sessions, statusline, warm-ups. If logic lives in both languages, the two copies drift.

## Decision
- **Swift** (`macos/`) owns only:
  - WidgetKit extension (rendering)
  - menu bar app and Settings window (UI)
  - native notifications (posting)
  - OS event observation (screen unlock, wake, login item)
  - calling the `ccs` CLI and reading or writing the files Python defines
- **Python** (`python/`, package `ccs`) owns all business logic: config validation, usage fetching, threshold and limit policy, supervisor state machine, pause/resume, statusline generation and rendering, warm-up scheduling and execution, auth wrapping, the daemon, and diagnostics.
- Swift never computes thresholds, colors levels, or policy outcomes. It renders what Python has already computed (`widget/snapshot.json` carries precomputed `level` values such as `green|yellow|red` and display strings where practical).
- **Allowed Swift-side computation:** pure presentation. That covers layout, localized date formatting of timestamps Python supplies, and relative-time strings for timeline entries.

## Consequences
- The Swift app is thin. Every action goes through `ccs <subcommand> --json`.
- Python must expose a machine-readable CLI (`--json` on every command the app calls) with stable output (see ADR-0005).
- Swift may read and write `config.json` directly (it is data, not logic), but must run `ccs config validate --json` after each write (ADR-0004).

## Rules for implementers
- Before adding logic in Swift, ask whether it is presentation. If not, it goes in Python and is exposed via the CLI or a state file.
- JSON contracts between the two sides live in `schema/` (JSON Schema). Change the schema first, then both sides.
