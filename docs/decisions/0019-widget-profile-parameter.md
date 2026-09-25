# ADR-0019: Widget profile parameter is a String with dynamic options

- Status: accepted
- Date: 2026-09-25
- Source: bug found after the first install (every widget showed the first profile). Supersedes the `ProfileEntity` part of ADR-0012.

## Context
- The widget's Edit Widget profile picker used an `AppEntity` (`ProfileEntity`) parameter.
- The system stores an entity choice as an `EntityIdentifier` and rebuilds it on every reload. That rebuild needs App Intents metadata from `linkd`.
- On the 26.x OSes `linkd` won't serve App Intents metadata to **ad-hoc signed** clients (no team identifier). Our builds are ad-hoc signed (ADR-0012), so the extension logged `Failed to build EntityIdentifier … is not a registered AppEntity identifier` on every reload. The parameter decoded to nil, and the system fell back to the default (first) profile for every widget.
- Plain parameter types (the Bool toggles) decoded fine.

## Decision
- The profile parameter is `profileID: String?` with a `DynamicOptionsProvider` (`ProfileOptionsProvider`).
  - Options come from `widget/snapshot.json`: value = profile id, title = `{emoji} {name}`.
  - The default is the first profile.
- No `AppEntity` types are used in the widget while builds are ad-hoc signed.
- `make app` unregisters every other `CC Supervisor.app` copy from LaunchServices (`scripts/lsclean.sh`), so macOS only sees the installed copy's metadata.

## Consequences
- Widgets configured before this change lose their profile choice once. Pick the profile again in Edit Widget.
- If the project ever moves to team signing, an `AppEntity` would work again, but there's no reason to switch back.
