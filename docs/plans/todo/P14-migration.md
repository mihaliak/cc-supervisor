# P14: Migration from current setup

- Status: todo (waiting for the user: it changes the real `~/.claude*` setup, which the user asked to leave untouched during implementation)
- Milestone: M3
- Depends on: P11, P13 (and transitively everything else)
- ADRs: [0003](../../decisions/0003-authentication.md), [0004](../../decisions/0004-config-and-profiles.md), [0009](../../decisions/0009-display-conventions.md), [0014](../../decisions/0014-docs-and-workflow.md), [0016](../../decisions/0016-identifiers.md)

## Goal
Move the user's real machine from the current setup (a shared bash statusline plus the third-party "Claude Usage.app") to CC Supervisor for both profiles. Retire the plaintext claude.ai cookie. Keep a working rollback.

**This plan runs on the real machine. Every step needs the user's explicit confirmation at execution time. Nothing is automated against dotfiles.**

## Current state (observed 2026-09-24)
- `~/.claude/settings.json` and `~/.claude-work/settings.json` both have `statusLine.command = "bash /Users/me/.claude/statusline-command.sh"`. Both profiles show the **personal** account's usage, which is a bug that motivates this project.
- `statusline-command.sh` reads:
  - `~/.claude/statusline-config.txt`
  - `~/.claude/.statusline-usage-cache`, written by "Claude Usage.app"
  - falls back to `swift ~/.claude/fetch-claude-usage.swift`
- `~/.claude/fetch-claude-usage.swift` contains a **plaintext claude.ai `sessionKey` cookie**, Cloudflare cookies, and an org id, injected by Claude Usage.app. **Secret. Never copy, archive, print, or commit it.**
- `~/.claude/settings.json` also has a SessionStart hook (Obsidian adopt-memory), plugins, and other keys that must be preserved.
- The dotfiles alias `claude-work='CLAUDE_CONFIG_DIR=~/.claude-work command claude'` is in `~/dotfiles/dots/.aliases`.
- `/Applications/Claude Usage.app` is installed.

## Scope
- Seed and verify profiles, verify sign-in, apply statuslines, retire Claude Usage.app, clean up the old files (archiving the non-secrets, deleting the secret), propose alias changes, verify, and roll back if needed.
- A `## Result` section recording what was done, including backup paths.

## Out of scope
- Editing dotfiles without approval.
- Deleting Claude config dirs, sessions, or logins.
- Any change to the `claude` installation.

## Design

### Step order (each step: show the command/diff → ask → run → verify)
1. **Pre-flight:** `ccs doctor` is all green except auth, and the menu bar app is running (P13 done).
2. **Profiles:**
   - `ccs profile list` must show `personal` (`~/.claude`, 🏠, default) and `work` (`~/.claude-work`, 💼), seeded by P02.
   - If one is missing: `ccs profile add --id work --flag work --name Work --emoji 💼 --config-dir ~/.claude-work` (similarly for `personal`), then `ccs config set default_profile=personal`.
   - The user confirms names and emoji, or changes them in Settings.
3. **Sign-in check:**
   - `ccs auth status --profile personal` and `--profile work` must both report `logged_in` with the expected accounts.
   - If not: `ccs auth login --profile <id>`.
   - `ccs usage --refresh` shows distinct numbers per profile.
4. **Retire Claude Usage.app** (before applying, so it stops rewriting its cache and statusline files):
   - Quit it.
   - Remove it from System Settings › General › Login Items.
   - The user deletes `/Applications/Claude Usage.app` (the user does it; it's a third-party app).
5. **Apply statuslines:**
   - `ccs statusline apply --profile personal`, then `--profile work`.
   - Record both `backup_path`s in `## Result`.
   - Verify with `jq .statusLine` on both `settings.json` files, and that the other keys are unchanged (`jq 'del(.statusLine)'` diff against the backup is empty).
6. **Live verification:**
   - `ccs --personal` and `ccs --work` in a scratch dir: each shows its own profile, emoji, and usage, and the numbers match `ccs usage`.
   - A plain `claude` (personal) shows the usage line without ⏸.
   - The menu bar and widgets show both profiles.
7. **Old files cleanup:**
   - Archive the **non-secret** files to `~/.local/state/ccs/migration/<YYYYmmddHHMMSS>/`: `statusline-command.sh`, `statusline-config.txt`.
   - Delete `~/.claude/.statusline-usage-cache`.
   - **Delete `~/.claude/fetch-claude-usage.swift` without archiving it.** Confirm it no longer exists.
   - Tell the user to **log out of claude.ai in the browser session that Claude Usage.app used** (claude.ai › Settings › log out of all sessions if unsure). This invalidates the exposed cookie. Deleting the file doesn't revoke it.
8. **Aliases (proposal only):** show a suggested diff for `~/dotfiles/dots/.aliases`. The user picks one of:
   - keep the name: `alias claude-work='ccs --work'` (plus `alias claude-personal='ccs --personal'`)
   - short: `alias cw='ccs --work'`, `alias cpers='ccs --personal'` (avoid `cp`, which shadows the copy command)
   - leave the aliases unchanged
   
   Apply only the approved variant, in the user's dotfiles repo. The user commits it there.
9. **Post-migration checklist:** see "Done when".

### Rollback
1. `ccs statusline revert --profile personal|work`, which restores the previous `statusLine` values exactly.
2. Restore the archived files from `~/.local/state/ccs/migration/<ts>/` to `~/.claude/`.
3. Reinstall Claude Usage.app if wanted. It regenerates its own cookie file after a fresh login.
4. If needed, `make uninstall` (P13).

## Tasks
- [ ] Pre-flight doctor run; result pasted into `## Result`.
- [ ] Step 2: profiles verified or added, with user confirmation.
- [ ] Step 3: both profiles signed in; distinct usage confirmed.
- [ ] Step 4: Claude Usage.app quit, login item removed, app deleted by the user.
- [ ] Step 5: statuslines applied to both config dirs; backups recorded; non-statusLine keys verified unchanged.
- [ ] Step 6: live verification of `ccs --personal`, `ccs --work`, plain `claude`, the menu bar, and widgets.
- [ ] Step 7: non-secrets archived; the cache and the secret file deleted; the user reminded to invalidate the claude.ai web session (and confirms it).
- [ ] Step 8: the alias proposal is shown; the user's choice is applied, or skipped.
- [ ] `## Result` written: timestamps, backup paths, the archive dir, the alias choice, any deviations.

## Tests
- Operational plan, with no new code. The verification commands in each step are its tests.
- If anything is scripted to help (for example a `jq` diff helper), it lives in `scripts/` and is run on copies first.

## Manual pages to update
- `01-installation.md`: a "Migrating from a custom statusline / Claude Usage app" section (generic, no personal paths)
- `11-troubleshooting.md` ("both profiles show the same usage", restoring a previous statusline)
- `README.md` (a one-line migration pointer)

## Done when
- [ ] Both profiles show their own usage in the statusline, the menu bar, and the widgets.
- [ ] `ccs doctor` is all ok.
- [ ] `~/.claude/fetch-claude-usage.swift` no longer exists, and the user confirmed the claude.ai web session was logged out.
- [ ] Claude Usage.app is gone, from the login items too.
- [ ] Backups and archive paths are recorded in `## Result`. A rollback was dry-read and confirmed possible.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **The cookie leaks during migration** → the file is never read, printed, archived, or committed. It is only deleted. The web session is invalidated.
- **Claude Usage.app rewrites the files after cleanup** → it's retired (step 4) before cleanup and apply.
- **Claude Code rewrites `settings.json` during apply** (a session open) → apply re-reads before writing (P07). Run with no active sessions, or re-verify afterwards.
- **The alias change breaks muscle memory or scripts** → proposal only. The `claude-work` name can be kept.
