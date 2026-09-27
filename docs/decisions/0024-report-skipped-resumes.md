# ADR-0024: Report skipped resume prompts

- Status: accepted
- Date: 2026-09-27
- Source: user decision ("report skipped resumes too"), after two sessions silently stayed stopped overnight

## Context
At a reset the daemon sends `resume` with the resume prompt to sessions that were busy when paused (ADR-0007). The launcher can still skip the prompt: the user typed while paused (ADR-0022 draft protection), Claude was still busy 30 s after the reset, or the command failed or never reached the launcher. `limit.resume` ("2 sessions continued") is emitted when the holds clear, before any ack, so a skipped prompt went unnoticed until the user checked the session.

## Decision
- The engine checks the launcher's ack of every `resume` that carried a prompt, automatic or manual. Anything but `injected` emits a **`limit.resume_skipped`** event, one per session:
  - `data`: `wrapper_id`, `cwd`, `reason`. The reason is the launcher's skip reason (`user_input`, `busy`), else the delivery result (`failed`, `timeout`, `not_connected`).
  - Not emitted when the session ended meanwhile.
- It is notified under the existing `limit_resume` toggle, never deduped. Text: "💼 Work: resume prompt not sent" — "roundly: you typed in it while it was paused. Continue it yourself."
- `limit.resume` is unchanged.
- This adds a type to ADR-0005's event list and a row to ADR-0015's notified events. Both are otherwise unchanged.

## Consequences
- A reset can bring two notifications: "resumed", then one "resume prompt not sent" per session that needs a nudge.
- Resumes without a prompt (idle when paused, overridden) never report.

## Rules for implementers
- Build the text in Python (`events.py`); the app posts it verbatim (ADR-0015).
