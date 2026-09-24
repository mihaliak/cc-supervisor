# P10: macOS app shell (menu bar, bridge, notifications, OS triggers)

- Status: todo
- Milestone: M2
- Depends on: P00 (S1: signing/data path outcome, S4: login mode), P04 (daemon socket, events, `widget/snapshot.json`)
- Uses when available: P06 (`pause`/`resume`), P08 (`warmup`), P09 (`auth login`). Buttons surface errors gracefully until those plans ship.
- ADRs: [0011](../../decisions/0011-macos-app.md), [0001](../../decisions/0001-language-split.md), [0005](../../decisions/0005-state-and-ipc.md), [0009](../../decisions/0009-display-conventions.md), [0010](../../decisions/0010-warmup.md), [0012](../../decisions/0012-widget-data-path.md), [0015](../../decisions/0015-notifications.md), [0016](../../decisions/0016-identifiers.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
A native menu bar app, `CC Supervisor.app`. It:
- shows live per-profile usage and supervisor state
- runs actions via `ccs`
- posts daemon events as native notifications
- forwards app-start, unlock, and wake as warm-up triggers
- keeps widgets fresh

## Scope
- XcodeGen app target in `macos/project.yml` (extends the P01 skeleton), `Info.plist`, and entitlements.
- `macos/App/`: the app entry, the menu bar UI, the bridge (`CcsClient`, `DaemonConnection`), `SnapshotStore`, `WidgetReloader`, `NotificationRouter`, `OSTriggers`, `LoginItemController`, `URLRouter`, and a minimal `ConfigReader`.
- `macos/Shared/` (compiled into the app **and** the widget): `Snapshot` Codable models, `TimeFormat`, `LevelColor`.
- A `Settings` scene placeholder, filled in P11.
- XCTest target `macos/Tests/AppTests`.

## Out of scope
- The Settings UI contents (P11) and the widget extension (P12).
- Any threshold, level, or policy computation (ADR-0001).
- Distribution and notarization (ADR-0011).

## Design

### Project
- App target `CCSupervisor`, bundle id `local.ccsupervisor.app`, product name `CC Supervisor`, macOS 26.0, Swift 6 language mode, strict concurrency complete.
- `Info.plist`: `LSUIElement = YES`, `CFBundleURLTypes` scheme `ccsupervisor`.
- Entitlements: no app sandbox. Hardened runtime is off for local builds.
- Signing per the P00-S1 outcome: ad-hoc (`CODE_SIGN_IDENTITY = "-"`), or a free team if the ADR-0012 fallback was adopted.
- `make app` (Makefile target from P01): `xcodegen generate` → `xcodebuild -scheme CCSupervisor -configuration Release` → copy to `~/Applications/CC Supervisor.app`.

### App entry `CCSupervisorApp`
- `MenuBarExtra(content: MenuContentView, label: MenuLabelView).menuBarExtraStyle(.window)`, plus `Settings { SettingsPlaceholderView }`.
- An `AppModel` (`@Observable @MainActor`) holds `SnapshotStore`, `DaemonConnection`, `CcsClient`, the ccs path, and the menu bar mode.

### `ConfigReader`
- Reads `~/.config/ccs/config.json` (honors `XDG_CONFIG_HOME`) for `ccs_path` (null → `~/.local/bin/ccs`) and `display.menu_bar`. Read-only here; P11 adds writes.

### `CcsClient` (`actor`)
- `run<T: Decodable>(_ args: [String], timeout: Duration = .seconds(30)) async throws -> T`.
- Uses `Process` with an absolute ccs path, always appends `--json`, and collects stdout/stderr asynchronously. On timeout it terminates the process and throws `.timeout`.
- Non-zero exit → `.failed(exitCode, stderrTail, parsedErrorJSON?)`.
- `PATH` for the child: prepend `~/.local/bin` and `/opt/homebrew/bin` to the inherited PATH (GUI apps get a minimal PATH).
- Typed helpers: `status()`, `usage(profile:refresh:)`, `sessions()`, `doctor()`, `authLogin(profile:)` (timeout 600 s), `authStatus(profile:)`, `warmup(profile:|all:trigger:)`, `pause(profile:)`, `resume(profile:)`, `daemonStatus()`, `daemonInstall()`, `daemonStart()`.
- Result structs mirror the P04/P06/P08/P09 JSON. Decode leniently (optionals), with fixtures in tests.

### `DaemonConnection` (`actor`)
- Uses `NWConnection` to `NWEndpoint.unix(path: <state>/daemon.sock)`.
- Framing: newline-delimited JSON with a reassembly buffer.
- On connect it sends `{"proto":1,"id":1,"op":"hello","client":"app","version":<app version>}`, then `{"proto":1,"id":2,"op":"subscribe","topics":["events","snapshot"]}`.
- Exposes `events: AsyncStream<DaemonEvent>`, `state: online|offline` (observable), and `send(op:)` for `refresh`.
- Reconnect backoff 1, 2, 5, 10, 30 s (capped), resetting after 60 s connected.
- While connected, the daemon uses native notifications (ADR-0015 fallback logic is daemon-side).

### `SnapshotStore` (`@Observable @MainActor`)
- Watches `<state>/widget/snapshot.json` using a `DispatchSource.makeFileSystemObjectSource` on the **directory** (atomic replaces swap the inode), plus a 30 s safety re-read.
- Decodes into `Snapshot` and publishes `snapshot`, `lastError`, and `generatedAt`.
- A snapshot push from `DaemonConnection` updates it immediately as well.

### `WidgetReloader`
- Calls `WidgetCenter.shared.reloadAllTimelines()` at most once per 60 s.
- Reloads **immediately** when any profile's `status` or `supervisor.state` differs from the last reloaded snapshot.
- Throttled changes schedule one trailing reload.
- Pure decision logic lives in `WidgetReloadPolicy` (testable with an injected clock).

### Shared presentation
- `TimeFormat.absolute(_ date: Date, now: Date, calendar:)` and `.relative(...)` implement ADR-0009 and must pass `schema/fixtures/time_format.json`.
- `LevelColor`: `green|yellow|red` → `Color`. No threshold math.

### Menu label `MenuLabelView`
- `emoji_percent`: for each profile, `"{emoji} {percent}%"` in level color. Missing data shows `?%`.
- `icon_only`: SF Symbol `gauge.with.dots.needle.50percent` tinted by the worst `level` across profiles.
- Colors may be flattened in the menu bar. If they are, render the label via `ImageRenderer` to a non-template `NSImage`.

### Menu content `MenuContentView`
- Daemon-offline banner:
  - `ccs daemon status --json` → not installed: **Install daemon** (`daemon install`)
  - installed but stopped: **Start daemon** (`daemon start`)
  - ccs missing: "ccs not found at <path>. Set the path in Settings".
- One `ProfileCardView` per profile:
  - header `emoji name`
  - status chip: ok / stale "updated 14m ago" / needs sign-in → **Sign in** button / source error
  - rows from `profile.rows[]`: label, percent, capsule bar (level color), `abs · rel` reset text (for extra usage, the row's `detail` string)
  - supervisor line: `N ccs sessions · M paused · K other`, and `Next warm-up 06:00` when present
- Actions:
  - per profile: **Warm up now** (`warmup --profile id --trigger manual`), **Pause/Resume** (label by `supervisor.state`)
  - global: **Refresh** (socket `refresh`, or `usage --refresh`), **Settings…** (`openSettings`), **Quit**
- Action errors show as an inline transient message on the card.

### `NotificationRouter`
- Requests `UNUserNotificationCenter` authorization (`.alert, .sound`) on first launch.
- For each `DaemonEvent` whose `data.notify == true` and which has `data.title` and `data.body`: post the content verbatim with identifier = event `key` (the same key replaces), `threadIdentifier` = profile id, and `userInfo["url"] = "ccsupervisor://profile/<id>"`.
- The delegate implements `willPresent → [.banner, .list]`, and `didReceive` → `URLRouter.open(url)`.
- No text is built in Swift (ADR-0015).

### `OSTriggers`
- On launch, once the daemon connection is online (or after 10 s), run `ccs warmup --all --trigger app_start` once.
- `DistributedNotificationCenter.default()` `"com.apple.screenIsUnlocked"` and `NSWorkspace.shared.notificationCenter` `didWakeNotification` → `ccs warmup --all --trigger unlock_wake`, debounced to at most one per 60 s.

### `LoginItemController`
- Wraps `SMAppService.mainApp` (`register`, `unregister`, `status`).
- On first launch (UserDefaults `didInitialLoginItemSetup`), register it.
- Command-line flag `--unregister-login-item`: unregister and exit (used by `make uninstall`, P13).

### `URLRouter`
- `ccsupervisor://profile/<id>` → set `AppModel.selectedProfileID` + `openSettings`. P11 selects the profile.
- `signin/<id>` → `CcsClient.authLogin`; show progress in that card.
- `refresh` → refresh.
- Unknown URLs are ignored and logged.

## Tasks
- [ ] `macos/project.yml`: app target settings, Info.plist keys, entitlements file `App/CCSupervisor.entitlements`, Shared sources group, AppTests target with the fixtures in `schema/fixtures/` as test resources.
- [ ] `Shared/Snapshot.swift`: Codable models for `widget/snapshot.json` (`schema: 1`), all non-essential fields optional.
- [ ] `Shared/TimeFormat.swift` + `Shared/LevelColor.swift`.
- [ ] `App/Bridge/CcsClient.swift` + result models `App/Bridge/CcsModels.swift`.
- [ ] `App/Bridge/DaemonConnection.swift` (NWConnection unix socket, JSON Lines, backoff).
- [ ] `App/Store/SnapshotStore.swift` + `App/Store/WidgetReloader.swift` (`WidgetReloadPolicy` pure).
- [ ] `App/Config/ConfigReader.swift`.
- [ ] `App/MenuBar/MenuLabelView.swift`, `MenuContentView.swift`, `ProfileCardView.swift`, `UsageRowView.swift`, `DaemonBannerView.swift`.
- [ ] `App/Notifications/NotificationRouter.swift`.
- [ ] `App/System/OSTriggers.swift`, `App/System/LoginItemController.swift` (+ `--unregister-login-item` handling in the app init).
- [ ] `App/URLRouter.swift` + `.onOpenURL` wiring.
- [ ] `App/Settings/SettingsPlaceholderView.swift`.
- [ ] Verify `make app` produces `~/Applications/CC Supervisor.app`. It launches with no Dock icon, and the menu shows fixture data when `CCS_STATE_DIR` points at the fixtures (debug env honored by `SnapshotStore`).

## Tests (XCTest, `macos/Tests/AppTests`)
- Decode every snapshot fixture in `schema/fixtures/snapshot/`: ok, stale, needs_sign_in, paused, no model-scoped, extra usage enabled.
- Decode `ccs … --json` fixtures (status, doctor, warmup results, auth).
- `TimeFormat` passes every vector in `schema/fixtures/time_format.json`.
- `WidgetReloadPolicy`: throttle window, immediate on state change, trailing reload.
- JSON Lines framer: split and partial messages.
- `URLRouter` parsing.

## Manual pages to update
- `04-menu-bar-app.md` (label modes, cards, actions, offline banner)
- `09-notifications.md` (permission prompt, click behavior)
- `01-installation.md` (login item, first launch)
- `11-troubleshooting.md` (ccs not found, daemon offline, no notifications)

## Done when
- [ ] `make app` builds and installs. XCTest passes.
- [ ] The menu bar shows live data from the real daemon and updates on snapshot changes.
- [ ] Notifications appear for daemon events that have `notify: true`, and clicking one opens the profile.
- [ ] Unlock, wake, and app start produce `warmup` calls (visible in `ccs events`).
- [ ] The login item is registered on first launch.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **The GUI app PATH lacks the user's tools** → absolute ccs path plus a PATH prefix. `ccs` itself resolves `claude` via config/daemon (ADR-0006).
- **Menu bar label color flattening** → the `ImageRenderer` fallback.
- **Notifications from an ad-hoc signed app get denied or suppressed** → verify in P00-S1. The daemon's osascript fallback covers it when no app is connected. Document it in troubleshooting.
- **The socket path is long** (unix path limit 104 bytes) → the state dir path is short (`~/.local/state/ccs/daemon.sock`). Assert its length in `DaemonConnection`.
