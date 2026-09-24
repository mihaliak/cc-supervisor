# Notifications

> Status: shipped (2026-09-24). The GUI parts (menu bar, Settings, widgets, native notifications) haven't had a human check yet.

CC Supervisor posts native macOS notifications from the menu bar app while it is connected to the background supervisor. Clicking one opens Settings on the profile it's about.

- The text is written by the supervisor; the app shows it exactly as sent.
- A newer notification about the same limit replaces the older one instead of stacking.
- Notifications of one profile are grouped together in Notification Center.

## What you get notified about
Each notification has a title and a body (shown here as *title* — *body*).

| When | Toggle | Example |
|------|--------|---------|
| A limit reaches its warn threshold | `limit_warn` | "💼 Work: session at 82%" — "Resets 20:00 (in 1h 12m)" |
| | | "💼 Work: weekly limit at 86%" — "Resets Sat 08:00 (in 1d 13h)" |
| | | "💼 Work: Fable weekly limit at 82%" — "Resets Sat 08:00 (in 1d 13h)" |
| | | "💼 Work: extra usage at 80% (€8.00 / €10.00)" — "Monthly credit cap" |
| Sessions were paused | `limit_pause` | "💼 Work paused at 90%" — "2 sessions paused. Resumes 20:00 (in 42m)" |
| | | "💼 Work paused at 95% (weekly limit)" — "1 session paused. Resumes Sat 08:00 (in 1d 13h)" |
| | | "💼 Work paused at 90% (extra usage)" — "1 session paused. Resumes when credits allow" |
| | | "💼 Work paused manually" — "3 sessions paused. Resume with: ccs resume --profile work" |
| Sessions were resumed | `limit_resume` | "💼 Work resumed" — "2 sessions continued" |
| A warm-up started a window, or failed | `warmup` | "💼 Work: session window started" — "Resets 11:02 (in 5h)" |
| | | "💼 Work: warm-up failed" — "claude did not answer within the timeout" |
| Sign-in needed, usage can't be read, config invalid | `errors` | "💼 Work: sign in required" — "Open CC Supervisor → Settings → Profiles → Work → Sign in, or run: ccs auth login --profile work" |

- Warn notifications cover the session, weekly, model-scoped (e.g. Fable), and extra-usage limits.
- A pause while no `ccs` session is running still notifies: "New ccs sessions will ask before starting. Resumes …". Its resume then reads "Limit reset; new sessions can start".
- Warm-ups notify only their **outcome**: the window started (with its reset time), or the warm-up failed (with the reason, e.g. `timeout`). Started and skipped warm-ups are only logged (`ccs events`).
- With **Fable warn-only** on, you get a notification at the warn threshold (80%) and again at 95% ("💼 Work: Fable at 95% (not pausing)"), instead of a pause.
- Overrides (typing into a paused session) are only logged (`limit.override` in `ccs events`), never notified.

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
macOS asks the first time the app starts. If you declined, enable it in System Settings → Notifications → **CC Supervisor**. Notifications also show while the menu is open or another app is in front (banner + Notification Center).

## No duplicates
- Each limit warns **once per window**, and pauses and resumes are announced once each.
- Error notifications come at most **once per hour** per profile and type.
- Everything is also kept in the event log: `ccs events`.

## When the app isn't running
The background supervisor still notifies you, through a basic macOS script notification (`osascript`), whenever no menu bar app is connected to it. It appears as coming from "Script Editor", shows the same title and body, and clicking it does nothing useful. Keep the app running at login for proper notifications. The toggles above apply to both kinds.
