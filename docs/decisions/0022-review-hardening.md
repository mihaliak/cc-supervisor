# ADR-0022: Review hardening

- Status: accepted
- Date: 2026-09-25
- Source: user decision ("fix it all" after the full bug/security review, P15)

## Context
A full review found behavior bugs where following the earlier ADRs word for word gives the wrong result. This ADR replaces only the clauses listed below. Everything else in those ADRs still applies.

## Decision

### ADR-0002: live supplement merge
- Only when the poll and the live report describe the **same window instance** (same `resets_at`, rounded to the minute), the **higher percent** wins. Usage only rises within a window, so a stale statusline redraw can't lower fresher poll data.
- Across different windows, the newest `observed_at` still wins.

### ADR-0004: config bounds
- `polling.*interval_seconds` are capped at 240 s, so the 5-minute heartbeat rule in ADR-0005 and ADR-0009 always holds.
- `claude_path` and `ccs_path` must be absolute or start with `~`.
- Path, name and id fields reject control characters. Prompt fields reject every control character except newline and tab.
- A warm-up `prompt` can't start with `-`, because `claude` would read it as an option.
- Every pattern (slugs, `HH:MM`) must match the whole string. A trailing newline is invalid.

### ADR-0006: process model
- If another daemon holds the lock when launchd starts the daemon, it waits for the lock instead of exiting. This stops the `KeepAlive` respawn loop.
- The LaunchAgent also passes `CCS_STATE_DIR`.
- The app reads `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, and `CCS_STATE_DIR` from the installed LaunchAgent's environment. It uses them for its own paths and for the `ccs` processes it spawns. Widgets still need the default state dir because of the sandbox exception, and `ccs doctor` warns when the state dir isn't the default.
- Session records loaded from disk at daemon start must re-register within a grace period, or the daemon reaps them. A live pid alone isn't enough, since pids get reused.
- The socket accepts ids only as `[A-Za-z0-9_-]{1,64}` and pids only within `1..2^31-1`. Clients refuse a state dir or socket that isn't owned by the current user.

### ADR-0007: pause/resume
- **Unknown status:** if `claude agents --json` gives no status (`unknown`), the launcher still injects ESC. It acks `was_busy: null`, though, so the session never gets an auto-resume prompt on a guess.
- **Override during a pause:** once the user submits while paused, the launcher stops injecting ESC for that pause.
- **Draft protection:** if the user typed anything while paused, even without submitting, the resume only clears the state. No prompt is injected, so it can't merge with a draft.
- **Started anyway:** the `overridden` start also covers model-scoped holds whose match can't be decided yet because `model_id` is unknown at registration. They become overridden once the model is known and matches.
- **Terminal restore:** on suspend and exit, the launcher resets the terminal modes the child enabled: kitty keyboard, modifyOtherKeys, focus reporting, bracketed paste, and mouse. On resume it re-enables them.

### ADR-0008: limit policy
- A session-scoped manual hold (`ccs pause --session X`) ends when session X ends.
- The `extra_usage` window instance is keyed by the cap and an arm counter, not the calendar month. It re-arms when `percent` drops below warn (a monthly reset or a raised cap), which matches the "Resume when" column.

### ADR-0010: warm-up confirmation
- The usage API can lag behind the warm-up request by over a minute. If the first forced poll shows no window, the runner polls again after 10, 20, 30, 60 and 60 s (about 3 minutes in total) before it reports `window_not_started`.

### ADR-0011: macOS app
- The `ccsupervisor://signin/<id>` deep link asks for confirmation before it starts sign-in.

## Consequences
- ADR-0002, 0004, 0006, 0007, 0008, 0010 and 0011 are partly superseded. Their status lines point here.
- Users with `interval_seconds` values above 240 get a validation error and must lower them.

## Rules for implementers
- Tests cover every clause above.
