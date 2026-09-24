# ADR-0007: Pause/resume via PTY keystroke injection

- Status: proposed. Confirm or replace after P00-S3.
- Date: 2026-09-24
- Source: user decisions (pause mechanism, pause scope, paused-input behavior)

## Context
Claude Code already waits at 100% and auto-continues after a reset (built-in quota auto-resume). The supervisor's job is to stop work **earlier** (at the configured pause threshold) so there is headroom left. It must keep the classic TUI open and resume automatically.

## Decision
- **Scope:** only interactive sessions launched via `ccs`.
  - Background agents (`claude --bg`, agent view) are not touched. Neither are plain `claude` sessions.
  - They are only counted and displayed, using `claude agents --json` per profile.
- **Busy detection:** run `CLAUDE_CONFIG_DIR=<dir> claude agents --json` and match `pid == claude_pid`. Its `status` is one of `busy|shell|idle|waiting`. Anything other than `idle` counts as busy for pausing.
- **Pause** (daemon sends `pause`):
  - If busy, the launcher injects `ESC` (0x1b) into the PTY master to interrupt the turn. It re-checks after 2 s and retries once, and records `was_busy_at_pause`.
  - If idle, it injects nothing and just marks the session paused.
  - The session record gets `state: paused`, its holds, and `resume_at`. The statusline shows `⏸ … paused → resumes HH:MM (in …)`.
- **Resume** happens when all holds are cleared, confirmed by a fresh poll after `resets_at`.
  - If `was_busy_at_pause`, the state is not `overridden`, and the session is idle, the launcher injects the profile's `resume_prompt` as a bracketed paste (`ESC[200~` … `ESC[201~`) followed by `\r`.
  - Otherwise it only clears the state.
- **Manual override** (user choice: "Allow = manual override"):
  - Input always passes through while paused.
  - If the user submits (a `\r` from the user's stdin while paused), the session becomes `overridden`: no auto-resume prompt, no re-interrupt for that window instance. Event `limit.override`.
- **New session while a hold is active:** the launcher asks before starting claude: `Profile work is paused until 20:00 (in 42m). Start anyway? [y/N]`. `--force` skips the question. If started anyway, the session begins as `overridden`.
- **Durability:** holds and session supervision state are persisted, so a daemon restart re-sends the current state to reconnecting launchers.

## Fallback (if S3 shows injection is unreliable)
"Stop & relaunch":
1. The launcher ends claude gracefully (`/exit` injection, then SIGTERM).
2. It shows a wait screen in the same terminal.
3. At reset it relaunches `claude --resume <session_id> "<resume_prompt>"`.

Adopting the fallback requires a superseding ADR.

## Rules for implementers
- Injection happens only on explicit daemon commands, never heuristically.
- Every injection is logged as a `wrapper_event`.
- Never inject while the user is mid-typing: if user input arrived less than 1.5 s ago, wait (up to 30 s) and then inject.
