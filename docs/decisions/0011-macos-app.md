# ADR-0011: macOS app: menu bar agent, macOS 26, local builds, XcodeGen

- Status: accepted; partly superseded by [ADR-0018](0018-menu-bar-label.md) and [ADR-0022](0022-review-hardening.md)
- Date: 2026-09-24
- Source: user decisions (menu bar presence, personal use, built locally, no stores) + planner defaults (XcodeGen, Swift 6)

## Decision
- **App:** `CC Supervisor.app`, a SwiftUI app with `LSUIElement` (no Dock icon). It has:
  - a `MenuBarExtra` in `.window` style
  - a `Settings` scene
  - a login item via `SMAppService.mainApp`
- **Targets:** macOS **26.0+** only, Swift 6 language mode, strict concurrency. The only external dependency is Apple SDKs.
- **Build:**
  - XcodeGen `macos/project.yml` generates `CCSupervisor.xcodeproj`. The generated project is gitignored.
  - Requires `brew install xcodegen`.
  - `make app` builds with `xcodebuild` and installs to `~/Applications/CC Supervisor.app`.
  - Personal use: no App Store, no notarization, no distribution.
- **Sandboxing:** the host app is **not** sandboxed, because it execs `ccs` and reads the state dir. The widget extension **is** sandboxed, as WidgetKit requires. Signing is covered in ADR-0012.
- **Menu bar label:** superseded by [ADR-0018](0018-menu-bar-label.md) (`letter_percent` default, `icon_only`).
- **Dropdown:**
  - per-profile cards: rows for Session, Weekly, model-scoped and Extra usage (%, bar, reset absolute + relative), plus supervisor state (active/paused ccs sessions, other sessions count, next warm-up)
  - actions: Refresh, Warm up now, Pause/Resume profile, Settings…, Quit
  - a banner when the daemon is offline, with an "Install / Start daemon" button
- **Python bridge:**
  - `CcsClient` runs `ccs <args> --json` via `Process`, with a timeout, and decodes typed `Codable` results.
  - `DaemonConnection` connects to `daemon.sock`, sends `subscribe`, and reconnects with backoff.
- **URL scheme** `ccsupervisor://`:
  - `profile/<id>` opens Settings on that profile
  - `signin/<id>` starts sign-in
  - `refresh` forces a poll

## Rules for implementers
- There is no business logic in Swift (ADR-0001).
- UI state comes from `widget/snapshot.json` (the same model the widgets use) plus daemon events.
- Every `Process` call runs off the main actor, with a 30 s default timeout, and surfaces errors in the UI.
