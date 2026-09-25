# ADR-0006: Process model: launchd daemon + PTY launcher

- Status: accepted; partly superseded by [ADR-0022](0022-review-hardening.md)
- Date: 2026-09-24
- Source: user decision ("ccs needs to open me classic claude code … it just needs to work as background supervisor") + planner design

## Context
`ccs --work` must feel exactly like running `claude`: the full interactive TUI in the current terminal. Supervision (polling, thresholds, pausing, warm-ups, notifications) must run once per machine, not once per terminal.

## Decision
- **`ccs daemon run`** is the single long-running supervisor.
  - Installed as a LaunchAgent (label `local.ccsupervisor.daemon`) with `RunAtLoad` and `KeepAlive`.
  - Its `PATH` and `HOME` are captured at `ccs daemon install` so it can find `claude`.
  - It is one asyncio event loop: socket server, per-profile pollers, policy engine, warm-up scheduler, config watcher, and a stale-session reaper.
  - A `flock` on `daemon.lock` guarantees a single instance.
- **`ccs --<flag> [claude args…]`** is the launcher.
  - It runs in the foreground of the user's terminal and spawns `claude [args…]` inside a PTY it owns (ADR-0007).
  - It sets `CLAUDE_CONFIG_DIR`, `CCS_PROFILE`, `CCS_WRAPPER_ID`, and `CCS_STATE_DIR`.
  - It adds `--settings '{"statusLine":{"type":"command","command":"<abs python> -S -E <config_dir>/ccs-statusline.py"}}'` when the profile's statusline is enabled. It regenerates the script first if it is missing or outdated. If the user passes their own `--settings`, the launcher does not inject. It prints a one-line notice before starting claude instead, and relies on `statusline apply`. That keeps the statusline active even if `apply` was never run.
  - It registers with the daemon and follows its pause/resume commands.
  - It exits with claude's exit code.
- **Profile selection:** the leading ccs options (`--<flag>` or `--profile <id>`, `--force`, `--no-supervise`) are consumed from the front of argv in any order, until the first other argument. With no profile option, the launcher uses `default_profile`. Everything after passes through to `claude` untouched.
- **Daemon availability:**
  - Launcher, daemon installed but not running: it starts it, using the same logic as `ccs daemon start` (bootstrap if unloaded, else kickstart).
  - Launcher, daemon not installed: it prints a one-line hint (`ccs daemon install`) and runs **unsupervised**. The statusline shows `⚠ supervisor offline`.
  - Launcher, daemon crashes mid-session: it reconnects with backoff and re-registers. Holds come back from `supervisor/<profile>.json`.
- **Management commands** are subcommands: `ccs status|usage|sessions|daemon|profile|config|auth|statusline|warmup|pause|resume|events|doctor`. Profile flags never collide with them because subcommands are positional.
- `--no-supervise` runs the launcher without registering, for debugging.
- **Non-interactive use:** when `-p`/`--print` is passed, or stdin/stdout isn't a TTY, the launcher execs `claude` directly, with no PTY and no supervision, so pipes and scripts keep working.

## Consequences
- The daemon is the only process that polls usage and the only one that runs warm-ups. The app and the launchers never poll.
- Terminal fidelity depends on the PTY proxy. It is validated in P00-S3.

## Rules for implementers
- The launcher must be transparent:
  - raw mode on the user's TTY, restored on every exit path (`try/finally` plus signal handlers)
  - forward `SIGWINCH` via `TIOCSWINSZ`
  - pass through all bytes unchanged, except for daemon-requested injections
- The launcher handles Ctrl-Z itself (ADR-0007 Verification): it strips it from input, self-suspends with the tty restored, and repaints on resume.
- Never write to the user's terminal while claude is running. Supervisor messages appear only in the statusline and in notifications.
- **Never set `CLAUDE_CONFIG_DIR` for the default `~/.claude`; unset it instead** (P05 finding, 2026-09-24). An explicit `CLAUDE_CONFIG_DIR=~/.claude` makes Claude Code keep its global state in `~/.claude/.claude.json` instead of `~/.claude.json`, with different trust, MCP servers, and onboarding. This applies to the launcher and to every `claude` call CC Supervisor makes (probes, `agents --json`, `auth`, warm-ups). Use `paths.apply_claude_config_dir()` / `claude_cli.profile_env()`.
