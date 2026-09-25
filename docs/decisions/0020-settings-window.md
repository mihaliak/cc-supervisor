# ADR-0020: Settings window layout

- Status: accepted
- Date: 2026-09-25
- Source: user feedback. The first Settings window's tabs didn't switch, its sidebar button didn't work, and it looked unfinished. Refines P11 and ADR-0011.

## Context
- The first window wrapped a `TabView` in a `VStack` (with banners above it), and the Profiles tab used a `NavigationSplitView`.
- Inside a Settings window, `NavigationSplitView` takes over the window toolbar. It added a sidebar toggle that did nothing and hid the tab switcher, so you couldn't get back to General.

## Decision
Follow the standard macOS Settings pattern (Mail, Safari, Xcode):
- **Root is a `TabView`**, which gives toolbar tabs; the window title is the selected pane. Panes:
  - **General:** launch at login, menu bar label, display (read-only)
  - **Profiles**
  - **Notifications**
  - **Advanced:** `ccs` path and version, daemon controls and logs, diagnostics
- **Fixed pane sizes.** The window isn't user-resizable and resizes per pane. The last viewed pane is restored.
- **Profiles pane** uses the Mail "Accounts" pattern, never `NavigationSplitView`:
  - a fixed, bordered profile list with the standard + / − bar attached below it
  - on the right, a header (emoji, name, `ccs --flag`, sign-in state) and a segmented control with four compact pages: **General** (profile fields, Claude account), **Limits** (supervisor, warn/pause grid, toggles), **Warm-up** (warm-up, when, schedule, status), **Statusline** (preview, apply/revert)
- **Forms** use `.formStyle(.grouped)` with section headers and footers. Explanations go in footers, not in extra rows.
- **Config banners** (missing, invalid, save errors) show at the top of each pane, and only when there is something to say.
- **No Save button.** Edits apply automatically (unchanged from P11).

## Rules for implementers
- No `NavigationSplitView` / `NavigationStack` inside the Settings scene.
- New settings go into the matching pane and page. Add a pane only for a new top-level area.
