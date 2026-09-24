# ADR-0015: Notifications

- Status: accepted
- Date: 2026-09-24
- Source: user requirement (macOS notification when the session limit is approaching) + planner design

## Decision
- The daemon emits events (ADR-0005). The **menu bar app** posts them as native notifications (`UNUserNotificationCenter`) under the app's identity. Clicking a notification opens `ccsupervisor://profile/<id>`.
- **Fallback:** if no app subscriber is connected, the daemon posts via `osascript -e 'display notification …'`, which is attributed to Script Editor.
- **Notified events** (each toggle lives in `config.notifications`):

| Event | Toggle | Example |
|-------|--------|---------|
| `limit.warn` | `limit_warn` | "💼 Work: session at 82%. Resets 20:00 (in 1h 12m)" |
| `limit.pause` | `limit_pause` | "💼 Work paused at 90%: 2 sessions. Resumes 20:00 (in 42m)" |
| `limit.resume` | `limit_resume` | "💼 Work resumed: 2 sessions continued" |
| `warmup.succeeded` / `warmup.failed` | `warmup` | "💼 Work: session window started, resets 11:02" |
| `auth.required`, `usage.source_error`, `config.invalid` | `errors` | "💼 Work: sign in required" |

- **Dedupe:** by event `key` (ADR-0008 window instance). Error notifications at most once per hour per profile and type.
- Model-scoped `warn_only` profiles get notifications at both warn (80) and pause-level (95) crossings.

## Rules for implementers
- Notification text is built in **Python**: events carry `data.title` and `data.body`. Swift posts them verbatim (ADR-0001).
- Python applies the toggles and dedupe. Every event carries `data.notify: bool`, and Swift and the osascript fallback post only `notify == true`.
