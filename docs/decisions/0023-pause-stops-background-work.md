# ADR-0023: Pause stops background agents and workflows

- Status: accepted (verified against Claude Code 2.1.283 on 2026-09-26; see Verification)
- Date: 2026-09-26
- Source: user decision ("it should pause main session and also its subagents and workflows")

## Context
ADR-0007 pauses a busy `ccs` session with ESC. ESC stops only the main turn: Claude Code skips background subagents and workflows on purpose. They keep running and using the window after the pause. Meanwhile `claude agents --json` reports the session as `busy` while any of them runs, even when the main turn is idle. So the pause sent two ESCs, acked `interrupted: false`, and the subagents and workflows kept working.

This ADR replaces ADR-0007's "only ESC and the resume prompt text are ever injected" rule. Everything else in ADR-0007 and ADR-0022 still applies.

## Decision
- **Pause, busy after the first ESC** (2 s later): the launcher sends ESC again, waits 0.5 s, then presses the `chat:killAgents` chord (`ctrl+x ctrl+k`) twice, which stops all background agents. The chord runs after a typing gap, because a key typed mid-chord would break it (ctrl+k alone deletes to the end of the line).
- **Still busy 2 s later** means workflows, which the chord doesn't stop. The launcher stops them through the prompt footer, one row at a time: Down × *n*, `x` (stop the selected row), then Backspace. Backspace either dismisses the stopped row, which clears the selection, or deletes the `x` again if it fell through into the prompt.
  - The row order isn't known, so each pass tries rows 1–4, checking status after each. It makes up to 2 passes, because a dismissed row moves the next one up.
  - The sweep runs only while the status is exactly `busy`, so never during a permission prompt (`waiting`).
  - It is skipped when the prompt may hold a draft (the user typed after their last submit).
  - It stops as soon as the user types or submits.
- **Still busy after that**: one more ESC. The "stopped by the user" notices start a turn of their own.
- **Resume:** the stopped work doesn't restart by itself. Claude reads "stopped by the user" as intended. When a pause got as far as the chord, the resume prompt gets a note appended: "Background agents or workflows stopped at the pause were stopped by the usage pause, not by the user: start again any that had not finished."
- Every injection is logged as a `wrapper_event` `injected`, with `what`: `esc` | `stop_agents` | `stop_workflow` | `resume_prompt`.

## Consequences
- ADR-0007 is partly superseded. Its status line points here.
- A pause with background work takes about 8–20 s instead of 2–4 s.
- If the user rebinds `chat:killAgents` or the footer keys in `keybindings.json`, stopping background work fails silently. The ack shows `interrupted: false`.
- Background shells (`status: shell`) are not stopped. They don't use the window.

## Verification (2026-09-26, Claude Code 2.1.283, headless PTY, Haiku)
- A backgrounded subagent (main turn idle) showed `busy`. ESC left it running. After the chord ("Press ctrl+x ctrl+k again to stop background agents" → "All background agents stopped"), the status was `idle` within 3 s. The "was stopped by the user" notice started a short main turn.
- A running workflow showed `busy` ("Waiting for 1 dynamic workflow to finish"). The chord didn't stop it. Down selected its row ("Enter to view · x to stop"), `x` stopped it ("✘ Stopped"), Backspace dismissed it, and the status was `idle`.
- With a background agent listed first, Down selected the agent pill, `x` fell through into the prompt, and Backspace removed it again. The next Down reached the workflow row.
- Source check: ESC runs `chat:cancel` with `suppressBackgroundAgentKill`. A session's status is `busy` while `isLoading || delegatedActive`, where delegatedActive means a running `local_agent`, `remote_agent`, `in_process_teammate` or `local_workflow`.

## Rules for implementers
- Keys are written one by one, `key_gap_s` (0.15 s) apart. ESC is never followed directly by other keys (`esc_gap_s`).
- Never inject slash commands. `/workflows` would work, but ADR-0007's rule stands.
- Tests use the fake `claude` (`after_stop_agents`, `after_stop_row`).
