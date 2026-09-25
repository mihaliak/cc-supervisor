# ADR-0005: State directory, file formats, daemon IPC

- Status: accepted
- Date: 2026-09-24
- Source: planner default

## Context
Several readers have different constraints:
- the statusline must be fast and file-only
- the widget is sandboxed and read-only
- the app needs push events
- the launchers need two-way commands

## Decision

### State dir: `~/.local/state/ccs/` (`$XDG_STATE_HOME/ccs/`)
```
daemon.sock                  unix socket, mode 0700 (owner-only)
daemon.lock                  flock, single daemon instance
logs/daemon.log              rotating (5 MB x 3)
usage/<profile_id>.json      UsageSnapshot (normalized, merged)
live/<wrapper_id|session-<session_id>>.json   statusline live reports
sessions/<wrapper_id>.json   supervised session record (statusline reads its own)
supervisor/<profile_id>.json profile holds, warn keys, pause ledger (survives restarts)
warmup/<profile_id>.json     last runs, next scheduled run
statusline/<profile_id>.json statusline apply bookkeeping (previous statusLine value, backup path) for revert
warmup/cwd/                  empty working dir for warm-up runs
widget/snapshot.json         display model for widgets + menu bar (all profiles)
events.jsonl                 append-only event log (rotate at 5 MB)
events.seen.json             event dedupe keys (≤ 1000, LRU)
```
- All JSON files carry `"schema": <int>`. Their schemas live in `schema/`.
- Writes are atomic (temp + `os.replace`). Readers tolerate a missing file and treat it as "no data".

### UsageSnapshot (`usage/<profile>.json`)
```jsonc
{
  "schema": 1,
  "profile_id": "work",
  "status": "ok",            // ok | needs_sign_in | no_subscription | source_error | stale
  "error": null,
  "fetched_at": "2026-09-24T16:07:33Z",     // last successful get_usage
  "polled_at": "2026-09-24T16:07:33Z",      // last poll attempt (success or failure)
  "subscription_type": "max",
  "windows": {
    "session": { "percent": 45, "resets_at": "2026-09-24T20:00:00Z", "observed_at": "…", "source": "get_usage|statusline" },
    "weekly":  { "percent": 50, "resets_at": "2026-09-26T06:00:00Z", "observed_at": "…", "source": "get_usage" },
    "model_scoped": [ { "name": "Fable", "percent": 4, "resets_at": "…", "observed_at": "…" } ]
  },
  "extra_usage": { "enabled": false, "percent": 0, "used": 0.00, "limit": 10.00, "currency": "EUR", "disabled_reason": "out_of_credits" }
}
```
- `stale` means no fresh data for more than 10 minutes.
- The daemon rewrites this file after **every** poll attempt, success or failure. On failure it keeps the last good windows and updates `status`, `error`, and `polled_at`. So the file's mtime doubles as the daemon heartbeat: older than 5 min means `supervisor offline` (ADR-0009).
- Money is converted from minor units using `decimal_places`.

### Live report (`live/<wrapper_id|session-<session_id>>.json`, written by the statusline)
- Format: `{schema: 1, profile_id, wrapper_id|null, session_id, model_id, effort|null, cwd, observed_at, rate_limits: {five_hour?: {percent, resets_at}, seven_day?: {…}}}`.
- It is rewritten when the content changes, or when it is 60 s old. The schema `schema/live-report.schema.json` is owned by P03.

### Session record (`sessions/<wrapper_id>.json`)
```jsonc
{
  "schema": 1, "wrapper_id": "uuid", "profile_id": "work",
  "wrapper_pid": 123, "claude_pid": 124, "session_id": "uuid|null", "cwd": "/…",
  "started_at": "…", "model_id": "claude-opus-5-5|null", "activity": "busy|idle|shell|waiting|unknown",
  "supervision": {
    "state": "running|paused|overridden",
    "holds": ["session", "weekly", "model_scoped:Fable", "extra_usage", "manual"],
    "paused_at": "…|null", "resume_at": "…|null", "was_busy_at_pause": true,
    "overridden_instances": ["session:2026-09-24T20:00:00Z"]
  }
}
```

### Widget snapshot (`widget/snapshot.json`)
- Precomputed by Python (ADR-0001).
  - Top level: `schema`, `generated_at`, `profiles[]`.
  - Per profile: `id, name, emoji, status, level, rows[]`, where each row has `kind, label, percent, level, resets_at, detail?`. `detail` is a preformatted string such as `€3.20 / €10.00`.
  - Also per profile: `supervisor` (`state, active_sessions, paused_sessions, other_sessions, resume_at, next_warmup_at`), `stale`, and `updated_at`.
- Swift formats only the dates (ADR-0009).
- It is rewritten after every poll attempt, success or failure. A `generated_at` older than 5 min means the daemon is offline, for the widgets and the menu bar.

### IPC: daemon unix socket, JSON Lines
- Each message is one JSON object per line and carries `"proto": 1`.
- Request/response: `{"id": n, "op": "...", ...}` → `{"id": n, "ok": true|false, "error"?: "...", ...}`.
- Ops:

| op | from | purpose |
|----|------|---------|
| `hello` | any | `{client: launcher|app|cli, version}` |
| `register_wrapper` | launcher | `{wrapper_id, profile_id, wrapper_pid, claude_pid, cwd, started_overridden}`. The reply includes active holds. |
| `unregister_wrapper` | launcher | `{wrapper_id, exit_code}` |
| `wrapper_event` | launcher | `{wrapper_id, kind: "input_submitted_while_paused" \| "injected" \| "inject_failed", detail}` |
| `subscribe` | app/cli | `{topics: ["events", "snapshot"]}`. The server then pushes `{"event": {...}}`. |
| `status` | any | full status (profiles, sessions, holds, next warm-ups) |
| `refresh` | any | force a poll: `{profile_id?}` |
| `warmup` | any | `{profile_id? \| all: true, trigger, force?}` |
| `pause` / `resume` | any | manual: `{profile_id? \| wrapper_id?}` |
| `reload_config` | any | reload immediately |

- Daemon → launcher commands are pushed on the launcher's connection as `{"cmd": {"cmd_id", "type": "pause" | "resume", ...}}`. The launcher replies `{"ack": cmd_id, "result": "injected" | "skipped" | "failed", "detail"}`.

### Events (`events.jsonl` + pushed to subscribers)
- Format: `{"ts", "type", "profile_id", "key", "data"}`. `key` is the dedupe key.
- Types:
  - `limit.warn`, `limit.pause`, `limit.resume`, `limit.override`
  - `warmup.started`, `warmup.skipped`, `warmup.succeeded`, `warmup.failed`
  - `auth.required`, `usage.source_error`, `config.invalid`
  - `session.started`, `session.ended`, `daemon.started`, `daemon.stopped`

## Consequences
- The statusline and widget never talk to the daemon. They read files only, so they keep working, with stale data, when the daemon is down.
- Every `ccs … --json` output and every state file is a versioned contract.

## Rules for implementers
- Bump `schema`/`proto` on breaking changes and keep readers backward-tolerant for one version.
- Never block the daemon event loop on subprocesses. Use asyncio subprocess with timeouts.
