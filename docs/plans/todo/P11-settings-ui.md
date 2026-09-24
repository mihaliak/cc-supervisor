# P11: Settings UI (profiles, sign-in, statusline apply)

- Status: todo
- Milestone: M2
- Depends on: P07 (`ccs statusline preview|apply|revert --json`), P09 (`ccs auth login|status|logout --json`), P10 (app shell, `CcsClient`, `ConfigReader`, `URLRouter`, `LoginItemController`)
- Also uses: P02 (`ccs config validate|defaults --json`, `ccs profile add|remove --json`), P04 (`ccs daemon status|install|start|stop|restart --json`), P13 (`ccs doctor --json`; if not shipped yet, the doctor section shows "unavailable")
- ADRs: [0004](../../decisions/0004-config-and-profiles.md), [0001](../../decisions/0001-language-split.md), [0003](../../decisions/0003-authentication.md), [0008](../../decisions/0008-limit-policy.md), [0009](../../decisions/0009-display-conventions.md), [0010](../../decisions/0010-warmup.md), [0011](../../decisions/0011-macos-app.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
A native Settings window where every profile setting from ADR-0004 can be viewed and edited. Profiles can be added and removed, and signed in or out, and each profile's statusline can be previewed, applied, and reverted. Config stays valid and conflict-safe with concurrent CLI and daemon writers.

## Scope
- `Settings` scene with two tabs, **General** and **Profiles**.
- `ConfigStore`: read and write `config.json` directly, with a revision check, unknown fields preserved, and an atomic replace. Post-write `ccs config validate --json` with inline errors.
- Profile create and remove through `ccs profile add|remove --json`, so Python owns the defaults.
- Sign-in section, statusline section, warm-up editor, and limits editor.
- Deep link `ccsupervisor://profile/<id>` selects the profile (routing from P10).

## Out of scope
- Any validation or policy logic in Swift. Swift displays `ccs config validate` output (ADR-0001).
- Widget configuration (P12, done via Edit Widget).
- Creating new Claude config dirs on disk. The picker selects existing ones; a missing dir is reported by validate or doctor.

## Design

### `ConfigStore` (`@Observable @MainActor`, `App/Config/ConfigStore.swift`)
- **Load:** read bytes → `JSONSerialization` into `raw: [String: Any]` (the source of truth for writing), and also decode `ConfigModel` (Codable, all fields optional) for display.
- **Edits:** typed setters write into `raw` via key paths (`["profiles", i, "limits", "session", "pause"]`). Each edit is recorded as a `PendingChange(path, value)`.
- **Save** (debounced 500 ms after the last edit, or immediately on explicit actions):
  1. Re-read the file. If `raw_disk["revision"] != loadedRevision`, reload, re-apply the pending changes onto the fresh raw, and retry once. If the same path changed on disk to a different value, show a conflict alert (keep mine / take theirs).
  2. Set `revision = diskRevision + 1`.
  3. Serialize with `JSONSerialization` options `[.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]` plus a trailing newline.
  4. Write to a temp file in the same dir, then `FileManager.replaceItemAt`.
  5. Run `ccs config validate --json` and map `issues[{path, message}]` onto fields (the path matches the key path string, e.g. `profiles[1].limits.session.pause`).
- Invalid values stay written. The daemon keeps the last good config (ADR-0004) until they're fixed. The UI shows a red banner: "Config invalid, daemon is using the previous version".
- **Unknown fields:** never dropped, because writes go through `raw`.

### General tab
- **ccs:** path field (`ccs_path`, placeholder `~/.local/bin/ccs`) with a Browse button, plus `ccs --version` output.
- **Menu bar:** picker `emoji_percent | icon_only` (`display.menu_bar`).
- **Notifications:** toggles `limit_warn`, `limit_pause`, `limit_resume`, `warmup`, `errors`, plus a "Open System Settings › Notifications" button.
- **Login item:** toggle via `LoginItemController`, with its status text.
- **Daemon:** status from `ccs daemon status --json` (installed, running, pid, uptime) and buttons Install / Start / Stop / Restart / Show logs (`ccs daemon logs` into a sheet).
- **Doctor:** "Run diagnostics" → `ccs doctor --json`, shown as a list of checks with ok/warn/fail icons and fix hints.
- **Display:** colors are read-only info ("Green < 50 %, yellow 50–79 %, red ≥ 80 %"), taken from `display.colors`. Editing them isn't exposed; it's rarely needed and can be done via `ccs config`.

### Profiles tab
- `NavigationSplitView`: a profile list (emoji + name + auth dot) with + / − buttons, and a detail editor.
- **Add:** a sheet with id, flag, name, emoji, and config dir → `ccs profile add --id … --flag … --name … --emoji … --config-dir … --json` → reload.
- **Remove:** a confirmation dialog → `ccs profile remove <id> --json`. The Claude config dir is not deleted, and the dialog says so.
- **Detail sections:**
  1. **Identity:**
     - id: read-only after create
     - flag: shows `ccs --<flag>`
     - name
     - emoji: text field plus a "😀" button calling `NSApp.orderFrontCharacterPalette(nil)` while the field is focused
     - config dir: path field plus Choose… (`NSOpenPanel`, `canChooseDirectories`, `showsHiddenFiles = true`, starting at `~`)
     - default profile: toggle that sets `default_profile`
  2. **Account:**
     - status from `ccs auth status --profile id --json`: logged in, account, subscription, keychain service (info)
     - **Sign in** → `CcsClient.authLogin` (600 s timeout, spinner, "Complete sign-in in your browser / the Terminal window"); after it, refresh the status
     - **Sign out** → a confirmation, then `ccs auth logout --profile id --json`
  3. **Limits:** steppers 1–100 for session warn/pause, weekly warn/pause, model-scoped warn/pause, and extra usage warn/pause.
     - Toggle **Model-scoped limits: warn only** (`model_scoped.warn_only`), with help text "Off: pause sessions on that model at the pause threshold. On: notify only".
     - Toggle **Spill into extra usage** (`extra_usage.spill`), with help text from ADR-0008.
     - Inline errors come from the validate output (for example warn < pause).
  4. **Supervisor:** toggle `supervisor.enabled` (help: "Off: warnings only, never pauses"), and a multi-line `resume_prompt` field.
  5. **Statusline:**
     - toggle `statusline.enabled`
     - preview list from `ccs statusline preview --profile id --json`: render `samples[].segments` as `AttributedString` in a monospaced font, mapping the segment `color` to `LevelColor` or gray
     - status: applied yes/no, script path, script current
     - **Apply** (`ccs statusline apply --profile id --json` → show `backup_path`) and **Revert** (`… revert …` → show `result`; `conflict` shows its hint)
  6. **Warm-up:**
     - toggle `warmup.enabled`
     - model: combo box, free text with suggestions `haiku, sonnet, opus, fable`
     - prompt: text field
     - triggers: `app_start`, `unlock_wake`, and `auto_chain` toggles
     - schedule rows: `DatePicker(.hourAndMinute)` + weekday chips Mon–Sun + remove, and an "Add time" button
     - active hours: two time pickers
     - cooldown: stepper 1–120 minutes
     - read-only "Next warm-up" and "Last attempt" from `ccs status --profile id --json`
- Time values are stored as `"HH:MM"` strings. Pickers convert using the local calendar, which is presentation only.

### Deep link
- `AppModel.selectedProfileID`, set by `URLRouter`, selects the list row when Settings opens or is already open.

## Tasks
- [ ] `App/Config/ConfigModel.swift` (Codable mirror of ADR-0004 v1, all optional) and `App/Config/ConfigStore.swift` (raw dictionary edits, revision check, conflict alert, atomic replace, debounce).
- [ ] `App/Config/ValidationErrors.swift`: decode `ccs config validate --json` and map it to field paths.
- [ ] Extend `CcsClient` with `configValidate()`, `profileAdd(...)`, `profileRemove(id)`, `statuslinePreview/apply/revert(profile)`, `authLogout(profile)`, `daemonStop/restart/logs()`.
- [ ] `App/Settings/SettingsView.swift` (TabView) + `GeneralSettingsView.swift`.
- [ ] `App/Settings/Profiles/ProfilesSplitView.swift`, `AddProfileSheet.swift`, and one view per detail section: `IdentitySection`, `AccountSection`, `LimitsSection`, `SupervisorSection`, `StatuslineSection`, `WarmupSection`.
- [ ] `App/Settings/Components/`: `EmojiField`, `DirectoryField`, `PercentStepper`, `WeekdayChips`, `SegmentsPreview` (ANSI-free segments → `AttributedString`).
- [ ] Replace P10's `SettingsPlaceholderView` and wire deep-link selection.
- [ ] Manual run-through on the real machine against the P00/P02 test profile: edit every field, check the diff in `config.json`, and confirm the daemon reloads (`ccs events` shows no `config.invalid`).

## Tests (XCTest)
- `ConfigStore` round-trip: load a fixture with unknown top-level, profile, and nested keys → edit one field → save → unknown keys are byte-equal as JSON values, and `revision` is incremented.
- Revision conflict: the disk revision changes a different field → the merge applies both. The same field → a conflict is reported (alert state set).
- The atomic write leaves no temp files, and the file mode is preserved.
- `ValidationErrors` mapping from fixture output to field paths.
- `SegmentsPreview` builds the expected `AttributedString` runs from a preview fixture.
- Weekday chips / `"HH:MM"` conversions round-trip across a DST date.

## Manual pages to update
- `02-profiles-and-sign-in.md` (add/remove profile, sign in/out in the app)
- `04-menu-bar-app.md` (Settings window overview)
- `06-statusline.md` (apply/revert/preview from the app)
- `07-limits-and-supervisor.md` (editing thresholds, warn-only, spill, supervisor toggle)
- `08-warm-up.md` (warm-up editor)
- `10-configuration-reference.md` (which fields are editable in the UI versus CLI-only)

## Done when
- [ ] Every ADR-0004 profile field and every General field listed above is editable. Edits persist with unknown keys preserved and pass `ccs config validate`.
- [ ] Add and remove profile, sign in and out, statusline preview/apply/revert, and daemon controls all work against the real `ccs`.
- [ ] Revision conflicts are handled as designed.
- [ ] `make app` builds. XCTest passes.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **Key-order and format churn between Swift and Python writers** → Swift writes `sortedKeys`, indent 2, trailing newline. P02's store should use the same (`sort_keys=True, indent=2, ensure_ascii=False`). If it doesn't, note a follow-up in P02 `## Result`.
- **The validate error path format differs from the key paths** → the mapping is table-tested against real `ccs config validate --json` fixtures.
- **A long-blocking sign-in** → async with a spinner, cancellable (terminates the `ccs` process), with the 600 s timeout.
- **Emoji field accepts multiple characters** → validate (Python) enforces the rules. The UI shows the error.
