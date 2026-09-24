# P09: Auth wrapper

- Status: todo
- Milestone: M1
- Depends on: P00 (S4: `claude auth login` without a TTY, `auth status --json` shape), P02 (config, paths), P03 (`claude_cli`: `resolve_claude`, `auth_status`), P04 (socket client, `refresh` op)
- Relies on: P04 (the poller emits `auth.required` on the `needs_sign_in` transition; text from `events.notification_text`, whose templates are in P06)
- ADRs: [0003](../../decisions/0003-authentication.md), [0001](../../decisions/0001-language-split.md), [0005](../../decisions/0005-state-and-ipc.md), [0013](../../decisions/0013-python-engineering.md), [0015](../../decisions/0015-notifications.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
Sign in, check, and sign out a profile's Claude Code login (claude.ai OAuth) through `ccs auth …`, so the app and terminal users never call `claude` directly. CC Supervisor stores no secrets.

## Scope
- Module `ccs/auth.py` (ADR-0016). It holds:
  - pure status parsing
  - keychain service name computation
  - login/logout orchestration via `ccs.claude_cli`
- CLI `ccs auth login [--terminal] | status | logout --profile <id> [--json]`.
- Login modes: interactive TTY, headless (no TTY, if P00-S4 shows it works), and a Terminal window.
- Post-login daemon `refresh`.
- A doctor helper for P13: `keychain_service_name(config_dir)`.

## Out of scope
- Reading Keychain items or tokens (forbidden, ADR-0003).
- The Sign in button UI (P11) and URL handling (P10).
- Implementing the `ccs doctor` command (P13).

## Design

### `keychain_service_name(config_dir: Path) -> str`
- `Path(config_dir).expanduser().resolve()`.
- If it equals `~/.claude` resolved: `"Claude Code-credentials"`.
- Otherwise: `f"Claude Code-credentials-{sha256(str(abs_path).encode()).hexdigest()[:8]}"`.
- Verified vector: `/Users/me/.claude-work` → `1e91dd84`.
- Pure function, no Keychain access.

### `parse_auth_status(raw: dict) -> AuthStatus`
- `AuthStatus(logged_in: bool, account: str | None, subscription_type: str | None, raw_keys: list[str])`.
- Field names come from P00-S4 fixtures: `loggedIn` (bool), `authMethod` (`claude.ai` | `none`), `apiProvider`, `analyticsDisabled`, `projectsDirectory`, `configDirectory`, `email`, `orgId`, `orgName`, `subscriptionType` (`max` | `team` | …). The last four are absent when logged out.
- `claude auth status --json` exits **1 when logged out**, and still prints the JSON.
- Parse defensively: unknown shape → `logged_in=False` plus `error`.
- `account` (email) is for display only and is never logged.

### `ccs auth status --profile <id> [--json]`
- Runs `claude auth status --json` with `CLAUDE_CONFIG_DIR` (timeout 15 s).
- Output: `{profile_id, config_dir, logged_in, account, subscription_type, keychain_service, error}`.
- Exit 0 even when logged out (it's a status). Exit 1 only when `claude` can't be run.

### `ccs auth login --profile <id> [--terminal] [--json]`
- **TTY on stdin and stdout, without `--terminal`:** run `claude auth login` with inherited stdio and `CLAUDE_CONFIG_DIR`, with no timeout, then run status.
- **No TTY** (for example, called by the app):
  - Headless mode, only if P00-S4 confirmed that `claude auth login` completes with `stdin=DEVNULL`, opening the browser and waiting for the callback. Capture output; timeout 600 s.
  - Otherwise it automatically falls back to terminal mode.
  - The choice is a constant `HEADLESS_LOGIN_SUPPORTED` in `auth.py`, set from S4's result. Swift never decides (ADR-0001).
- **S4 result: `HEADLESS_LOGIN_SUPPORTED = True`.**
  - `claude auth login --claudeai` with `stdin=DEVNULL` prints `Opening browser to sign in…` plus the URL and calls `open <url>`, resolved via `PATH`.
  - The OAuth `redirect_uri` is `http://localhost:<port>/callback`, so it completes without a TTY once the user consents.
  - Always pass `--claudeai` so no method prompt can appear.
- **Terminal mode** (`--terminal`, or the automatic fallback):
  1. Write `<state>/tmp/signin-<profile>.command` (mode 0700) containing `#!/bin/zsh -l` and `exec "<abs ccs path>" auth login --profile <id>`.
  2. Run `open -a Terminal <file>` (no Apple Events/Automation permission needed).
  3. Poll `auth status` every 3 s for up to 600 s. Delete the file afterwards.
- **After success:** send the daemon socket `refresh {profile_id}` (ignore it if the daemon is down).
- **`--json` output:** `{profile_id, mode: "tty"|"headless"|"terminal", logged_in, account, error}`. Exit 1 if not logged in at the end.

### `ccs auth logout --profile <id> [--json]`
- Human mode prompts `Log out <emoji> <name> (<config_dir>)? [y/N]`.
- `--json` means the caller has already confirmed. The app shows its own confirmation dialog (P11).
- Runs `claude auth logout` with `CLAUDE_CONFIG_DIR`, then refresh. Output: `{profile_id, logged_in: false, error}`.

### `auth.required`
- Emitted by the **P04 poller** on the transition `ok|unknown → needs_sign_in` (key `auth:<pid>:<YYYYmmddHH>`, at most once per hour per profile, ADR-0015).
- Its `title`/`body`/`notify` come from `EventBus.emit` via `events.notification_text` (P06 templates): title `💼 Work: sign in required`, body `Open CC Supervisor → Settings → Profiles → Work → Sign in, or run: ccs auth login --profile work`.
- This plan adds no emitter or text. It only adds an integration test that `ccs auth login` clears the state (next poll → `ok`).

### Safety
Every test runs against fake_claude with temp config dirs. No command here is ever run against the real `~/.claude*` in tests.

## Tasks
- [ ] Use the P00-S4 fixtures (scrubbed) at `python/tests/fixtures/auth_status/{logged_in,logged_out}.json`.
- [ ] `ccs/auth.py`: `keychain_service_name`, `parse_auth_status`, `AuthStatus`, `HEADLESS_LOGIN_SUPPORTED`.
- [ ] `ccs/claude_cli.py`: use P03's `auth_status(claude, profile)`, and add `auth_login(claude, profile, mode)` and `auth_logout(claude, profile)` (IO shells; `claude` from `resolve_claude(cfg)`).
- [ ] Terminal-mode helper: `.command` file writer, `open -a Terminal`, poll loop, cleanup (the `open` call is injectable for tests).
- [ ] CLI `ccs auth login|status|logout` subcommands, TTY detection (`sys.stdin.isatty() and sys.stdout.isatty()`), human and `--json` output, exit codes.
- [ ] Post-login daemon `refresh` via the socket client (P04); no-op when the daemon is offline.
- [ ] Integration test: fake logged-out probe → `auth.required` event (from P04) → `ccs auth login` (fake) → next poll `ok`.
- [ ] fake_claude: `auth status --json` scenarios (logged in / logged out / garbage), `auth login` (succeeds after N seconds, fails), `auth logout`.

## Tests
- `keychain_service_name` vectors: `~/.claude` → no suffix; `~/.claude-work`, expanded to the test HOME, → sha prefix matches `hashlib`; trailing slash normalized.
- `parse_auth_status` on each fixture plus an unknown shape.
- Login mode selection matrix: TTY / no-TTY × `--terminal` × `HEADLESS_LOGIN_SUPPORTED`.
- Terminal mode with a stubbed `open`: file content, mode 0700, polling success and timeout, file removed.
- Logout confirmation prompt (human) and `--json` no-prompt.
- After login, the next poll returns `ok` and the profile leaves `needs_sign_in` (P04 owns the emission tests).

## Manual pages to update
- `02-profiles-and-sign-in.md` (sign-in flow, what the Terminal window is, sign out)
- `05-ccs-cli.md` (`ccs auth …`)
- `11-troubleshooting.md` ("sign in required", login window doesn't open, wrong account)

## Done when
- [ ] `ccs auth status|login|logout` work against fake_claude in all modes.
- [ ] The keychain service name vector matches.
- [ ] No test or code path reads the Keychain or touches real config dirs.
- [ ] The daemon refreshes after login, and the profile's status returns to `ok`.
- [ ] `make test lint` passes.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **`claude auth login` changes its flow or prompts** (for example a login-method picker) → terminal mode always works as the interactive fallback, and S4 records the prompts seen.
- **Signing into the wrong account in the browser** → status shows `account` after login. The UI (P11) displays it prominently.
- **The Terminal `.command` file exposes the ccs path only**, no secrets. It is deleted after use.
