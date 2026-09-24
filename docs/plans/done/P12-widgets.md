# P12: Widgets (small/medium/large)

- Status: done
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
- [x] `macos/project.yml`: `CCSupervisorWidgets` app-extension target (WidgetKit, SwiftUI), sandbox entitlements per the S1 outcome (`Widgets/CCSupervisorWidgets.entitlements`), embedded in the app, Shared sources included, the same signing as the app.
- [x] `Widgets/Data/SnapshotLoader.swift` (real-home resolution, error cases).
- [x] `Widgets/Intent/ProfileEntity.swift`, `ProfileQuery.swift`, `ProfileWidgetIntent.swift`.
- [x] `Widgets/Timeline/UsageEntry.swift`, `UsageTimelineProvider.swift`, `EntryViewModel.swift` (pure: snapshot + intent + date → display model, including the state table above).
- [x] `Widgets/Views/HeaderView.swift`, `UsageRow.swift`, `UsageBar.swift`, `StateOverlay.swift`, `SmallWidgetView.swift`, `MediumWidgetView.swift`, `LargeWidgetView.swift`.
- [x] `Widgets/CCSupervisorWidgetsBundle.swift`: widget kind `ProfileUsageWidget`, `AppIntentConfiguration`, supported families small/medium/large, display name "Claude usage", description.
- [x] `#Preview`s for each family × state using fixtures from `schema/fixtures/snapshot/` (add missing variants: paused, needs_sign_in, stale, offline, spill with extra usage).
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
- [x] Every state in the table renders correctly (previews + tests).
- [x] No thresholds or level math in Swift. There are no process, network, or file writes in the extension.
- [ ] `make app` builds. XCTest passes. Any Python contract changes pass `make test lint`.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **The ad-hoc signed widget doesn't load, or the exception is denied** → the ADR-0012 fallback A (a free team + App Group), decided in P00-S1 before this plan starts.
- **Desktop widgets desaturate when inactive** (vibrant mode) → percent text is always shown. Colors are an enhancement.
- **Reload throttling** → per-minute precomputed entries plus P10's immediate reload on state changes. Documented in the manual.
- **Snapshot schema drift between Python and Swift** → shared fixtures in `schema/fixtures/snapshot/`, used by both test suites.

## Result
Shipped 2026-09-24.

**What shipped**
- Widget kind `ProfileUsageWidget` ("Claude usage"): `AppIntentConfiguration` with `ProfileWidgetIntent` (Profile, Show weekly, Show model limits, Show extra usage), families small/medium/large, `widgetURL` → `ccsupervisor://profile/<id>`. App Intents metadata (`ProfileWidgetIntent`, `ProfileEntity`) is extracted into the appex.
- Pure display logic in `macos/Shared/Widget/` (compiled into app + widget, tested by `CCSupervisorTests`):
  - `SnapshotLoader` (`missing` / `unreadable` / `decode` / `sandboxDenied` via the POSIX cause; EPERM = sandbox),
  - `WidgetDisplayBuilder` (snapshot + profile id + options + date → `WidgetDisplay`: state table, rows, weekly footer, paused/resume, supervisor line, next warm-up, updated text, footer, dim, URL; family row capacity and drop order),
  - `WidgetTimeline` (now + 59 whole-minute entries), `WidgetProfileCatalog` (Edit Widget choices, default = first), `WidgetSamples` (gallery/placeholder/previews).
- Widget target `macos/Widgets/`: `Intent/` (entity, query, intent), `Timeline/` (entry, provider: snapshot loaded once, 60 entries, `.after(last)`; gallery without data → sample), `Views/` (header, bar with `.widgetAccentable()`, compact/stacked rows, weekly line, state message, footer, small/medium/large, `#Preview`s for 6 states × 3 sizes), `ProfileUsageWidget.swift` (@main bundle). Placeholder widget removed.
- Fixtures added: `schema/fixtures/snapshot/{offline,spill,no_data}.json` (validated by the Python schema test, whose required-name list now includes them).
- Tests: `macos/Tests/WidgetDisplayTests.swift`, 23 tests (every state, paused, toggles, per-family rows + drop priority, per-minute countdown incl. `in <1m` → `now`, stale turning on across entries, all `time_format.json` vectors through the widget path, loader errors, catalog, samples). `make test lint`: Swift suite green; Python 698 passed / 1 skipped; ruff + mypy clean. `make app-build` has no warnings.

**Deviations**
- **Offline is judged when the timeline is built, not per entry.** Later entries can't know whether the daemon died, and macOS may throttle reloads, so per-entry evaluation would falsely show "Supervisor offline" 5 min after every reload. Data age (stale: `stale` flag / status, or `updated_at` > 10 min) is still judged per entry date, so a widget macOS hasn't refreshed shows "updated Xm ago" dimmed. If the daemon stops, the widget goes stale within 10 min and shows offline at its next reload.
- **Tests live in the existing `CCSupervisorTests` target** (host = app), not a separate `WidgetTests` target: an app extension can't host XCTest, so all pure logic sits in `Shared/Widget/` and is tested there.
- **View names** `WidgetUsageBar` / `CompactUsageRowView` / `StackedUsageRowView` / `StateMessageView` / `FooterView` instead of `UsageRow`/`UsageBar`/`StateOverlay` (`UsageRow` is the Shared data model); views grouped in `Views/Components.swift` + `Views/FamilyViews.swift`.
- **`SnapshotLoader` in `Shared/Widget/`** (not `Widgets/Data/`) so tests can reach it.
- **Previews use `WidgetSamples`** (code-built snapshots mirroring the fixtures) because the extension doesn't bundle `schema/fixtures/`.
- **Extra states:** "no data" (no rows: `No usage data yet` / `Usage unavailable` / `No plan limits for this account`), and a missing snapshot with a selected profile = offline with "No usage data yet". Needs-sign-in taps open `profile/<id>` (ADR-0003/0012), same as every other state; not-configured has no URL (opens the app).
- **Supervisor line** omits "0 paused"; `1 ccs session` singular.
- Toggle titles follow the manual ("Show model limits").

**Deferred to the user (needs the GUI; not verifiable headless)**
- The widget appears in the gallery (search "CC Supervisor" / "Claude usage") and renders real data for both profiles on the desktop in all three sizes.
- Right-click → Edit Widget lists the profiles (from `widget/snapshot.json`) and the three toggles; changing them updates the widget.
- Clicking opens Settings on the profile; the paused badge / resume line appear during a real pause.
- Refresh after the app's `WidgetReloader` reloads (and how much macOS throttles it).
- Requires the app installed (`make app`, not run here) and the daemon writing `~/.local/state/ccs/widget/snapshot.json`.

