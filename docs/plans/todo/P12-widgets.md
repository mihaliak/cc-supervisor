# P12: Widgets (small/medium/large)

- Status: todo
- Milestone: M2
- Depends on: P06 (supervisor fields in the snapshot), P10 (Shared models, `TimeFormat`, `LevelColor`, `WidgetReloader`, URL routing), P00-S1 (signing and data path outcome → ADR-0012 confirmed or fallback)
- ADRs: [0012](../../decisions/0012-widget-data-path.md), [0001](../../decisions/0001-language-split.md), [0004](../../decisions/0004-config-and-profiles.md), [0005](../../decisions/0005-state-and-ipc.md), [0009](../../decisions/0009-display-conventions.md), [0011](../../decisions/0011-macos-app.md), [0016](../../decisions/0016-identifiers.md)

## Goal
Desktop and Notification Center widgets, one profile per widget. Each shows:
- session usage (% with a green/yellow/red bar, absolute and relative reset)
- weekly usage
- model-scoped usage (for example Fable) and extra usage when present
- supervisor state

Each widget is configured by right-click → Edit Widget (profile + row toggles).

## Scope
- Widget extension target `CCSupervisorWidgets` (`local.ccsupervisor.app.widgets`), embedded in the app.
- `SnapshotLoader` (sandboxed read path per S1), `UsageTimelineProvider`, `ProfileWidgetIntent`, `ProfileEntity`, `ProfileQuery`.
- Views: `SmallWidgetView`, `MediumWidgetView`, `LargeWidgetView`, and shared `UsageRow`, `UsageBar`, `HeaderView`, `StateOverlay`.
- Fixture-driven previews and tests.
- Consumes the snapshot contract as guaranteed by P04/P06 (ADR-0005): top-level `generated_at`, `supervisor.other_sessions`, `supervisor.resume_at`, and `rows[].detail`.

## Out of scope
- Refresh triggering (P10 `WidgetReloader`).
- Any threshold or level computation (ADR-0001).
- Interactive buttons that run actions. Taps only open URLs.

## Design
- **P00-S1 findings:**
  - An ad-hoc signed widget extension registers with `pluginkit` (`com.apple.widgetkit-extension`), and the system launches it sandboxed (its container was created).
  - The `home-relative-path.read-only` temporary exception is honored under ad-hoc signing: the allowed path reads OK, other home files are denied.
  - Inside the sandbox `NSHomeDirectory()`/`homeDirectoryForCurrentUser` is the **container** path. Resolve the real home with `getpwuid(getuid())->pw_dir`.
  - Desktop/gallery rendering couldn't be observed headless and is deferred to the user's manual check.
  - Keep data access behind one `SnapshotLocation` abstraction, so App Group mirroring (ADR-0012 fallback A) is a build-setting switch (`CCS_WIDGET_DATA=appgroup`).

### Data path (from P00-S1)
- **Primary:** the sandbox entitlement `com.apple.security.temporary-exception.files.home-relative-path.read-only = ["/.local/state/ccs/widget/"]`. Read `~/.local/state/ccs/widget/snapshot.json`. Resolve the real home via `getpwuid(getuid())`, because the sandbox `HOME` points into the container.
- **Fallback A:** App Group `<TEAMID>.local.ccsupervisor`. Read `snapshot.json` from `FileManager.containerURL(forSecurityApplicationGroupIdentifier:)`. The host app mirrors it (add the mirror step to P10's `SnapshotStore` if this path is chosen).
- `SnapshotLoader.load() -> Result<Snapshot, LoadError>` with errors `.missing`, `.unreadable`, `.decode`, and `.sandboxDenied`.

### Configuration: `ProfileWidgetIntent: WidgetConfigurationIntent`
- `@Parameter profile: ProfileEntity?`
- `@Parameter showWeekly: Bool = true`
- `@Parameter showModelScoped: Bool = true`
- `@Parameter showExtraUsage: Bool = true`
- The toggles affect medium and large only. Small always shows session plus a compact weekly line.
- `ProfileEntity(id, name, emoji)`. `ProfileQuery.entities(for:)` and `.suggestedEntities()` read the snapshot's profiles. `defaultResult()` is the first profile.

### Timeline: `UsageTimelineProvider: AppIntentTimelineProvider`
- Load the snapshot once, then build 60 entries, one per minute from now.
- Each entry has `date` plus precomputed strings via `TimeFormat` for every row reset, next warm-up, resume time, and "updated Xm ago".
- Reload policy: `.after(lastEntry.date)`.
- `placeholder`: a redacted fixture. `snapshot(for:in:)`: the real data, or the fixture in the gallery.

### Entry-level states (evaluated per entry date)
| State | Condition | UI |
|-------|-----------|----|
| not configured | no profile selected, or the id isn't in the snapshot | "Choose a profile" (right-click → Edit Widget) |
| daemon offline | top-level `generated_at` is older than 5 min at the entry date (the daemon rewrites the snapshot on every poll attempt, ADR-0005), or the snapshot is missing | last values dimmed + footer "Supervisor offline" |
| needs sign-in | `profile.status == "needs_sign_in"` | header + "Sign in required", tap URL `ccsupervisor://profile/<id>` (opens the profile's settings with the Sign in button, per ADR-0003/0012) |
| stale | `profile.stale == true` or `updated_at` older than 10 min | values dimmed (opacity 0.5) + "updated 14m ago" |
| ok | otherwise | normal |

- Paused badge: when `supervisor.state == "paused"`, show `⏸ Paused` in the header, plus `resumes 20:00 · in 42m` (large, if `resume_at` is present).
- `widgetURL(ccsupervisor://profile/<id>)` for the whole widget, except in the sign-in state.

### Layouts
Text uses the ADR-0009 compact form `20:00 · in 2h 13m`.
- **Small** (`.systemSmall`):
  - header `emoji name` (+ paused badge)
  - large session percent in level color, with a capsule bar below it
  - `20:00 · in 2h 13m`
  - footer `W 50% · Sat 08:00`, with the percent in level color
- **Medium** (`.systemMedium`):
  - header row
  - up to 4 `UsageRow`s: Session, Weekly (if `showWeekly`), each model-scoped row (if `showModelScoped`), and Extra usage (if `showExtraUsage` and the row exists)
  - each row: label | percent | bar | reset text, where the extra usage row shows `rows[].detail` (e.g. `€3.20 / €10.00`) instead of a reset
  - rows are dropped by priority when space runs out: Extra, then model-scoped, then Weekly
- **Large** (`.systemLarge`): the medium rows plus a supervisor section:
  - `3 ccs sessions · 1 paused · 2 other`
  - `Next warm-up 06:00 · in 9h 12m`
  - `Updated 1m ago`
- Colors: `LevelColor` for level strings only. Apply `.widgetAccentable()` to bars. In vibrant or accented rendering modes the percent text still carries meaning.

### Refresh note (manual)
macOS may throttle widget reloads. The menu bar app is the real-time view. Countdowns stay accurate between reloads because of the per-minute entries.

## Tasks
- [ ] `macos/project.yml`: `CCSupervisorWidgets` app-extension target (WidgetKit, SwiftUI), sandbox entitlements per the S1 outcome (`Widgets/CCSupervisorWidgets.entitlements`), embedded in the app, Shared sources included, the same signing as the app.
- [ ] `Widgets/Data/SnapshotLoader.swift` (real-home resolution, error cases).
- [ ] `Widgets/Intent/ProfileEntity.swift`, `ProfileQuery.swift`, `ProfileWidgetIntent.swift`.
- [ ] `Widgets/Timeline/UsageEntry.swift`, `UsageTimelineProvider.swift`, `EntryViewModel.swift` (pure: snapshot + intent + date → display model, including the state table above).
- [ ] `Widgets/Views/HeaderView.swift`, `UsageRow.swift`, `UsageBar.swift`, `StateOverlay.swift`, `SmallWidgetView.swift`, `MediumWidgetView.swift`, `LargeWidgetView.swift`.
- [ ] `Widgets/CCSupervisorWidgetsBundle.swift`: widget kind `ProfileUsageWidget`, `AppIntentConfiguration`, supported families small/medium/large, display name "Claude usage", description.
- [ ] `#Preview`s for each family × state using fixtures from `schema/fixtures/snapshot/` (add missing variants: paused, needs_sign_in, stale, offline, spill with extra usage).
- [ ] On the real machine: add each size to the desktop, configure the profile via Edit Widget, and verify data, taps, and the updates after the P10 reload.

## Tests (XCTest target `WidgetTests`)
- `EntryViewModel` per state: not configured, offline, needs sign-in, stale, ok, paused. Row visibility per toggle and family. The row drop priority in medium.
- Time strings per entry across 60 minutes (a relative string decreases, and `in <1m` → `now`), using `schema/fixtures/time_format.json`.
- `SnapshotLoader` decode for all fixtures. Missing and corrupt file → the correct `LoadError`.
- `ProfileQuery` returns snapshot profiles, and `defaultResult` is the first.

## Manual pages to update
- `03-widgets.md` (adding widgets, Edit Widget options, sizes, states, refresh behavior)
- `README.md` (feature list and screenshot placeholders)

## Done when
- [ ] Small, medium, and large widgets appear in the gallery and render real data for both profiles on the desktop.
- [ ] Edit Widget offers the profile and toggles. Taps open the right URLs.
- [ ] Every state in the table renders correctly (previews + tests).
- [ ] No thresholds or level math in Swift. There are no process, network, or file writes in the extension.
- [ ] `make app` builds. XCTest passes. Any Python contract changes pass `make test lint`.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **The ad-hoc signed widget doesn't load, or the exception is denied** → the ADR-0012 fallback A (a free team + App Group), decided in P00-S1 before this plan starts.
- **Desktop widgets desaturate when inactive** (vibrant mode) → percent text is always shown. Colors are an enhancement.
- **Reload throttling** → per-minute precomputed entries plus P10's immediate reload on state changes. Documented in the manual.
- **Snapshot schema drift between Python and Swift** → shared fixtures in `schema/fixtures/snapshot/`, used by both test suites.
