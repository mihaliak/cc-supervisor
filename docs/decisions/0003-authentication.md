# ADR-0003: Authentication: Claude Code's own login per config dir

- Status: accepted
- Date: 2026-09-24
- Source: user decision

## Context
Each profile maps to a Claude Code config dir (`CLAUDE_CONFIG_DIR`). Claude Code stores that dir's OAuth credentials in the macOS Keychain:
- `~/.claude` → service `Claude Code-credentials`
- any other dir → service `Claude Code-credentials-<sha256(absolute dir path)[:8]>`. Example: `/Users/me/.claude-work` → `1e91dd84`.

## Decision
- "Sign in via claude.ai" means running `claude auth login` with `CLAUDE_CONFIG_DIR=<profile.config_dir>`. This is claude.ai OAuth in the browser, wrapped as `ccs auth login --profile <id>`.
- Auth status comes from `claude auth status --json` (wrapped as `ccs auth status --profile <id> --json`).
- CC-Supervisor stores **no secrets**. It never reads Keychain items; it only computes the service name for diagnostics in `ccs doctor`.
- The widget and menu bar show `needs_sign_in` when `get_usage` or `auth status` reports no auth. Clicking opens the profile's settings, which has a Sign in button.

## Consequences
- Signing in through the app also signs in plain `CLAUDE_CONFIG_DIR=... claude` for that dir. This is intended: one login per profile.
- Whether `claude auth login` works without a TTY (launched from the GUI app) is verified in P00-S4. If it doesn't, the app opens a terminal window running `ccs auth login --profile <id>`.

## Rules for implementers
- All auth actions go through `ccs auth ...`. Swift never execs `claude` directly.

## Verification (P00-S4, 2026-09-24)
- **Headless login works.** With a temp config dir and `stdin=DEVNULL`, `claude auth login --claudeai` prints `Opening browser to sign in…` plus the URL, runs `open <url>` (resolved via `PATH`), and waits on an OAuth `redirect_uri=http://localhost:<port>/callback`. No TTY prompt appears.
  - The default login path is therefore **headless** (`HEADLESS_LOGIN_SUPPORTED = True`), with `--claudeai` always passed.
  - `--terminal` stays as a manual fallback.
  - Completion after browser consent was not exercised; that needs the user.
- **`claude auth status --json`:**
  - Fields: `loggedIn`, `authMethod` (`claude.ai` | `none`), `apiProvider`, `analyticsDisabled`, `projectsDirectory`, `configDirectory`.
  - When logged in, it also has `email`, `orgId`, `orgName`, `subscriptionType`.
  - It exits **1 when logged out**, and still prints the JSON.
  - Fixtures: `python/tests/fixtures/auth_status/{logged_in,logged_in_team,logged_out}.json`.
