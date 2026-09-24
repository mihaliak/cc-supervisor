# Notifications

> Status: planned. This page describes target behavior and will be marked "shipped" when implemented (see [../plans/backlog.md](../plans/backlog.md)).

CC Supervisor posts native macOS notifications from the menu bar app. Clicking one opens Settings on the profile it's about.

## What you get notified about
| When | Toggle | Example |
|------|--------|---------|
| A limit reaches its warn threshold | `limit_warn` | "💼 Work: session at 82%. Resets 20:00 (in 1h 12m)" |
| Sessions were paused | `limit_pause` | "💼 Work paused at 90%: 2 sessions. Resumes 20:00 (in 42m)" |
| Sessions were resumed | `limit_resume` | "💼 Work resumed: 2 sessions continued" |
| A warm-up started a window, or failed | `warmup` | "💼 Work: session window started, resets 11:02" |
| Sign-in needed, usage can't be read, config invalid | `errors` | "💼 Work: sign in required" |

- Warn notifications cover the session, weekly, model-scoped (e.g. Fable), and extra-usage limits.
- With **Fable warn-only** on, you get a notification at the warn threshold (80%) and again at 95%, instead of a pause.

## Turning them on or off
Settings → General → Notifications, or in the config:
```json
"notifications": {
  "limit_warn": true, "limit_pause": true, "limit_resume": true,
  "warmup": true, "errors": true
}
```
These toggles apply to all profiles.

## Permission
macOS asks the first time the app starts. If you declined, enable it in System Settings → Notifications → **CC Supervisor**.

## No duplicates
- Each limit warns **once per window**, and pauses and resumes are announced once each.
- Error notifications come at most **once per hour** per profile and type.
- Everything is also kept in the event log: `ccs events`.

## When the app isn't running
The background supervisor still notifies you, through a basic macOS script notification. It appears as coming from "Script Editor" and clicking it does nothing useful. Keep the app running at login for proper notifications.
