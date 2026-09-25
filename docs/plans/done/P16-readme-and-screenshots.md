# P16: README and screenshots

- Status: done
- Milestone: M3
- Depends on: P15
- ADRs: [0001](../../decisions/0001-language-split.md), [0011](../../decisions/0011-macos-app.md), [0013](../../decisions/0013-python-engineering.md), [0014](../../decisions/0014-docs-and-workflow.md)

## Goal
A GitHub README for `mihaliak/cc-supervisor`: logo, features, installation, screenshots of every feature, and license with an AI-assistance note. Screenshots are generated, never taken from personal data.

## Decisions
- User decisions (2026-09-25): render screenshots ourselves (no Screen Recording permission), repo `mihaliak/cc-supervisor`, MIT license.

## Scope
- `README.md`, `LICENSE`, `.github/workflows/{python,swift}.yml` (badges).
- `make screenshots`, which runs these steps:
  - `scripts/screenshots/demo_env.py` builds an anonymized demo HOME, config and state.
  - `capture_cli.sh` records `ccs` output.
  - `macos/Screenshots/` renders the real SwiftUI views and the captures into `docs/assets/screenshots/`.

## Out of scope
- Publishing the repo, releases, Homebrew.

## Design
- **Harness build:** it compiles the app, shared and widget view sources with `-D SCREENSHOTS`. `AppEnvironment.isRunningTests` is then true, so it reads only the demo env and runs no daemon, notifications or login items. `AppModel.showDaemonOnline()` hides the offline banner.
- **Rendering:**
  - Pure SwiftUI (widgets, terminal) goes through `ImageRenderer`.
  - Views with AppKit controls (menu, Settings) are drawn from an offscreen window with `cacheDisplay`.
  - Window chrome, the Settings toolbar and the menu bar are drawn around the real content.
- **Anonymity:** the Advanced pane is left out because its diagnostics show absolute paths. The captures fail if they contain the real username, `/Users/` or a non-example email.

## Tasks
- [x] Demo env and CLI captures
- [x] Render harness and `make screenshots`
- [x] README, LICENSE, workflows
- [x] Verify the images (content, anonymity)

## Tests
- `make test lint` stays green. The harness is checked by running `make screenshots`.

## Manual pages to update
- None (the README links to `docs/manual/`).

## Done when
- The README renders with logo, badges, features, installation, screenshots and license; `make screenshots` regenerates every image; no personal data appears in any image.

## Risks
- Offscreen AppKit rendering draws toggles and checkboxes in the inactive-window style.
- The badges only work once the repo is pushed to GitHub with Actions enabled, and a `macos-26` runner is needed.

## Result
- **Shipped:**
  - `README.md`: logo, badges, features, installation, screenshots, license and a vibe-coded note.
  - MIT `LICENSE` and CI workflows `python.yml` / `swift.yml` (`macos-26`).
  - `make screenshots`, producing 18 PNGs, about 4.3 MB: 8-bit, opaque, flat backgrounds.
- **Tooling:**
  - The demo env and CLI captures run a real demo daemon on the demo state, with a `launchctl` stub.
  - Captures fail on the real username, `/Users/`, or a non-example email.
- **App changes:**
  - The Statusline section shows the script path with `~`.
  - `-D SCREENSHOTS` hooks: `isRunningTests`, `realHome()` follows `$HOME`, and `AppModel.showDaemonOnline()`. None of them exist in the app build.
- **Deviations:**
  - The General and Advanced panes aren't shown. General describes the harness binary (icon, version, login item), and Advanced's diagnostics list absolute paths.
  - Toggles render in the inactive-window style.
- **Follow-ups:**
  - Badges go live once the repo is pushed to `mihaliak/cc-supervisor` with Actions enabled.
  - Confirm that the `macos-26` runner has Xcode 26.
