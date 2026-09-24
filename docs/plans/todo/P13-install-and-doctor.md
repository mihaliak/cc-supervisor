# P13: Install, uninstall, doctor

- Status: todo
- Milestone: M3
- Depends on: P06, P07, P08, P09, P12 (and transitively P01–P05, P10)
- ADRs: [0006](../../decisions/0006-process-model.md), [0003](../../decisions/0003-authentication.md), [0004](../../decisions/0004-config-and-profiles.md), [0005](../../decisions/0005-state-and-ipc.md), [0011](../../decisions/0011-macos-app.md), [0012](../../decisions/0012-widget-data-path.md), [0013](../../decisions/0013-python-engineering.md), [0016](../../decisions/0016-identifiers.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
One command installs everything on this Mac. One command removes it cleanly. `ccs doctor` explains exactly what is wrong and how to fix it.

## Scope
- Makefile targets `install`, `uninstall`, `upgrade` (they compose the P01 targets `install-dev`, `app`, and so on).
- `scripts/check-prereqs.sh`, a POSIX sh prerequisite checker used by `make install`.
- `ccs/doctor.py` plus `ccs doctor [--json]`.
- App flag `--unregister-login-item` (implemented in P10). This plan only uses it.

## Out of scope
- Distribution, notarization, Homebrew (ADR-0011: personal local builds only).
- Migrating the user's existing setup (P14).
- Auto-fixing: doctor only reports fix hints. Fixes are explicit commands.

## Design

### `make install`
1. `scripts/check-prereqs.sh`, which fails fast with a hint for each missing item:
   - macOS ≥ 26
   - Xcode ≥ 26 (`xcodebuild -version`)
   - `xcodegen` (`brew install xcodegen`)
   - `python3` ≥ 3.12 on PATH
   - `pipx`
   - `claude` on PATH (a warning only)
2. `pipx install --editable ./python` (or `pipx reinstall` if already installed) → `~/.local/bin/ccs`.
3. `ccs config validate` (the first run seeds the config per P02).
4. `ccs statusline generate` for every profile with `statusline.enabled`. The scripts are needed by the launcher. `apply` is **not** automatic; it's a P14/user step.
5. `make app` → `~/Applications/CC Supervisor.app`.
6. `ccs daemon install` (writes the LaunchAgent with the captured PATH/HOME, then bootstraps it).
7. `open "~/Applications/CC Supervisor.app"` (registers the login item on first launch and asks for notification permission).
8. `ccs doctor`.
9. Print the next steps: sign in to profiles, `ccs statusline apply --profile <id>`, add widgets.

### `make uninstall` (interactive, with confirmations)
1. For each profile that has statusline bookkeeping: `ccs statusline revert --profile <id>`. Report conflicts; never force.
2. `ccs daemon uninstall` (bootout, remove the plist).
3. `"~/Applications/CC Supervisor.app/Contents/MacOS/CC Supervisor" --unregister-login-item`, then quit the app (`osascript -e 'quit app "CC Supervisor"'`), then remove the app bundle.
4. `pipx uninstall cc-supervisor`.
5. Prompt `Also delete config (~/.config/ccs) and state (~/.local/state/ccs)? [y/N]`. Only an explicit `y` deletes. Generated `ccs-statusline.py` files in the config dirs are listed and removed only on the same `y`.
6. The Claude config dirs and their logins are never touched.

### `make upgrade`
1. `pipx reinstall cc-supervisor` (editable: refresh the entry points).
2. `make app`.
3. `ccs daemon restart`.
4. `ccs statusline generate` for all profiles (refresh the embedded sources and interpreter path).
5. `ccs doctor`.

### `ccs doctor`
- Output model:
  - `Check(id, scope: "global" | "profile:<id>", status: ok|warn|fail, message, fix: str | None)`
  - `--json` → `{"checks":[…], "summary":{"ok":n,"warn":n,"fail":n}}`
  - human output: grouped, with ✓ / ! / ✗ and a fix line
  - exit 1 if any check is `fail`
- **Global checks:**
  - `python.version` ≥ 3.12 (fail)
  - `ccs.version` (info as ok)
  - `claude.found`: resolved path (config `claude_path` or the PATH captured by the daemon install) (fail)
  - `claude.version` (ok/warn if unknown)
  - `config.valid` (fail with the validate errors)
  - `config.reserved_flags`: parse the long options in `claude --help`. Warn if any are missing from the static reserved list in `ccs.config.validate`, and fail if any collides with a profile `flag`.
  - `daemon.installed` (plist exists), `daemon.running` (launchctl print + pid alive), `daemon.socket` (connect + `hello` round-trip < 1 s) (fail)
  - `notifications.route`: daemon `status` reports an app subscriber connected (ok), or none → warn "osascript fallback in use"
  - `app.installed`: `~/Applications/CC Supervisor.app` exists (warn)
  - `widget.snapshot`: `widget/snapshot.json` exists and `generated_at` < 5 min (warn). With the App Group fallback, also check the mirrored copy's mtime.
- **Per-profile checks:**
  - `config_dir.exists` (fail)
  - `auth.status`: via P09 `auth status`. Logged out → warn with fix `ccs auth login --profile <id>`.
  - `auth.keychain_service`: the computed service name (info, never read)
  - `usage.last_poll`: liveness is `usage/<profile>.json` `polled_at` age < 5 min (warn otherwise: daemon not polling). Freshness is `fetched_at` age < 10 min (warn otherwise: polls failing). `status == ok` (warn or fail by status).
  - `statusline.script`: present, with a header generator version and sources hash equal to the current values (warn → `ccs statusline generate --profile <id>`)
  - `statusline.interpreter`: the embedded interpreter path exists (fail)
  - `statusline.applied`: `settings.json` `statusLine.command` equals `statusline_command(script)`. Warn if not applied, info if a different statusLine is set.
- Every check has a timeout (subprocess 10 s) and never raises. Unexpected exceptions become a `fail` whose message is the exception type.

## Tasks
- [ ] `scripts/check-prereqs.sh` (POSIX sh, `set -eu`, one line per check, non-zero on a hard fail).
- [ ] Makefile `install`, `uninstall`, `upgrade` targets as designed. Confirmation prompts use `read` in sh. `uninstall` supports `YES=1` for scripted runs, but the purge still requires `PURGE=1`.
- [ ] `ccs/doctor.py`: a `Check` dataclass, a registry of check functions, the runner with timeouts, and formatters (human, JSON).
- [ ] Global checks listed above.
- [ ] Per-profile checks listed above (reusing P07 `ensure_script` internals read-only, P09 `auth_status` / `keychain_service_name`, the P04 socket client).
- [ ] `config.reserved_flags` parser for `claude --help` (regex `--([a-z][a-z0-9-]+)`), with a fixture of the current help text.
- [ ] CLI `ccs doctor [--json]`.
- [ ] A dry run of `make install` / `make uninstall` on this machine with an **isolated HOME** (`HOME=$(mktemp -d)` plus a stubbed launchctl via `LAUNCHCTL=echo`), to validate the scripts without touching the real setup.

## Tests
- `doctor` checks with temp dirs + fake_claude:
  - everything healthy → all ok
  - missing config dir
  - logged out
  - a stale script (generator version bump)
  - a missing interpreter
  - statusline not applied / a foreign statusLine
  - daemon socket missing
  - stale snapshot
  - `claude --help` with a new flag colliding with a profile flag
- JSON output schema, and the exit code 1 on a fail.
- A check timeout produces a `fail`, not a hang.
- `check-prereqs.sh` run with a stubbed PATH (missing xcodegen → non-zero, with the hint).

## Manual pages to update
- `01-installation.md` (prerequisites, `make install`, first launch, upgrade, uninstall)
- `05-ccs-cli.md` (`ccs doctor`)
- `11-troubleshooting.md` (every doctor check id → meaning → fix)
- `README.md` (quick start)

## Done when
- [ ] `make install` on a clean isolated HOME ends with `ccs doctor` all ok, except auth warnings.
- [ ] On the real machine it installs the app, daemon, and CLI, and the menu bar shows data.
- [ ] `make uninstall` restores the `settings.json` statuslines, removes the daemon, app, and login item, and leaves the Claude config dirs untouched. Purge only on explicit confirmation.
- [ ] `ccs doctor` covers every listed check, with fix hints.
- [ ] `make test lint` passes. `make app` builds.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **launchctl domain and bootstrap differences** → use `launchctl bootstrap gui/$(id -u)` / `bootout`, idempotent. The P04 daemon install already handles this; reuse it.
- **pipx editable installs break when the repo moves** → doctor `ccs.version` shows the source path, and `make upgrade` reinstalls.
- **Uninstall leaves stale login items** → `--unregister-login-item` runs before the bundle is deleted. The manual shows how to remove leftovers in System Settings › General › Login Items.
