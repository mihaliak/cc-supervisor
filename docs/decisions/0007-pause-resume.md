# ADR-0007: Pause/resume via PTY keystroke injection

- Status: accepted (verified by P00-S3 on 2026-09-24; see Verification); partly superseded by [ADR-0022](0022-review-hardening.md) and [ADR-0023](0023-pause-stops-background-work.md)
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

## Verification (P00-S3, 2026-09-24, headless PTY harness)
- **Worked:**
  - An ESC injected into the PTY interrupted a busy turn in 0.21 s ("Interrupted · What should Claude do instead?").
  - A bracketed paste plus `\r` submitted a prompt.
  - `claude agents --json` mapped the child pid to `status: busy` within 0.5 s, then `idle` after ESC.
  - Resize propagated.
  - Exit code with the proxy matched a direct run (0).
  - Throughput was 95 MB/s through the proxy vs 137 MB/s direct.
  - The first-run folder-trust dialog passes through the proxy unchanged.
- **Additional rules learned:**
  - **Ctrl-Z must be handled by the launcher.** The PTY child is a session leader in an orphaned process group, so claude's own suspend is a no-op: it shows "suspended" but keeps running, and the shell never regains control. The launcher strips Ctrl-Z (`\x1a` and kitty `CSI 122;5u`), suspends itself with the tty restored, and on `SIGCONT` re-raws and nudges the winsize to force a repaint. Verified.
  - **Never inject slash commands.** `/model <x>` persists `model` into the profile's `settings.json`. Only ESC and the resume prompt text are ever injected.
- **Not verifiable headless** (deferred to the user's manual check, P05 task): visual fidelity in Ghostty and Terminal.app, mouse scrolling, and kitty keyboard-protocol keys.
