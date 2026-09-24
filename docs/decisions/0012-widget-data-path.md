# ADR-0012: Widget data path & signing

- Status: accepted (primary path verified as far as possible headless by P00-S1 on 2026-09-24; see Verification)
- Date: 2026-09-24
- Source: user decision ("spike ad-hoc first") + planner design

## Context
- There are no code-signing identities on this Mac.
- WidgetKit extensions must be sandboxed.
- App Groups normally need a team-signed provisioning profile. Free Apple ID profiles expire after 7 days.
- On macOS 15+, a non-group process that touches another app's Group Container triggers a privacy prompt. So Python must not write into a Group Container.

## Decision
- **Primary (ad-hoc, no Apple ID):**
  - Sign both targets with "Sign to Run Locally" (ad-hoc).
  - The widget extension entitlements are `com.apple.security.app-sandbox` plus `com.apple.security.temporary-exception.files.home-relative-path.read-only = ["/.local/state/ccs/widget/"]`.
  - The widget reads `~/.local/state/ccs/widget/snapshot.json` directly.
- **Fallback A (if ad-hoc widgets don't load, or the exception is denied):**
  - Sign with a free Apple ID personal team and use App Group `<TEAMID>.local.ccsupervisor`.
  - The host app (a group member) mirrors `snapshot.json` into the group container on every change.
  - Weekly re-sign via `make app`, documented in the manual.
- **Timeline and reload:**
  - The provider emits one entry per minute for the next 60 minutes. Each entry carries precomputed relative strings.
  - Reload policy is `.after(lastEntry)`.
  - The host app watches `snapshot.json` (`DispatchSource` file monitor) and calls `WidgetCenter.shared.reloadAllTimelines()`, throttled to at most 1 per 60 s. It reloads immediately on status changes (paused, resumed, sign-in).
- **Configuration:** `AppIntentConfiguration`.
  - `ProfileEntity`: its query lists profiles from `snapshot.json`.
  - Toggles: `showWeekly`, `showModelScoped`, `showExtraUsage` (medium and large only).
  - Tapping the widget opens `ccsupervisor://profile/<id>`.
- **Sizes:** small, medium, large (see P12).

## Consequences
- macOS may throttle widget refresh. The menu bar remains the real-time surface. The manual must say so.

## Rules for implementers
- The widget never runs processes, never writes files, and never makes network calls.

## Verification (P00-S1, 2026-09-24)
- **Worked:**
  - Ad-hoc signed (`CODE_SIGN_IDENTITY=-`, no team) app plus widget extension built with Xcode 26.2 and XcodeGen 2.46.
  - `pluginkit` registered the extension as `com.apple.widgetkit-extension`, and the system launched it sandboxed (its container was created).
  - An ad-hoc signed sandboxed tool with the same `home-relative-path.read-only` exception could read `~/.local/state/ccs/widget/snapshot.json`, and was denied `~/.zshrc`.
- **Implementation notes:**
  - Inside the sandbox, `NSHomeDirectory()` is the container. Resolve the real home via `getpwuid(getuid())->pw_dir`.
  - macOS keeps the containers `~/Library/Containers/<bundle id>` after uninstall. They can't be removed with `rm` (containermanagerd).
- **Not verifiable headless:** the widget showing up in the gallery and rendering on the desktop. The user checks this manually after P12.
- **Fallback:** fallback A needs the user's Apple ID and was not exercised. Keep a single `SnapshotLocation` abstraction so App Group mirroring can be switched by a build setting.
