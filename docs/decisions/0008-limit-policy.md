# ADR-0008: Limit policy & default thresholds

- Status: accepted; partly superseded by [ADR-0022](0022-review-hardening.md)
- Date: 2026-09-24
- Source: user decisions (80/90 session, weekly 80/95, model-scoped toggle, extra-usage toggle) + planner defaults (hysteresis, cadence)

## Decision

### Windows and defaults (per profile, ADR-0004)
| Window | Warn | Pause | Pause affects | Resume when |
|--------|------|-------|---------------|-------------|
| `session` (five_hour) | 80 | 90 | all ccs sessions of the profile | window reset confirmed |
| `weekly` (seven_day) | 80 | 95 | all ccs sessions of the profile | weekly reset confirmed |
| `model_scoped` (e.g. Fable), `warn_only: false` (default) | 80 | 95 | only ccs sessions whose current `model_id` contains the scope name, lowercased (e.g. `fable`) | that window's reset confirmed |
| `model_scoped`, `warn_only: true` | 80 | – (a second notification at 95 instead) | none | – |
| `extra_usage`, `spill: false` (default) | 80 % of monthly cap | – | none (display + warn only) | – |
| `extra_usage`, `spill: true` and credits enabled | 80 % of cap | 90 % of cap | all ccs sessions | `percent` drops below pause (monthly reset or raised cap) |

- **Spill mode:** when `spill: true` and `extra_usage.enabled == true`, the **session and weekly pauses are suppressed** (their warnings still fire), because the work continues on credits. If credits are not enabled (for example `disabled_reason: out_of_credits`), spill is ignored and the normal pauses apply.
- **Comparison:** `percent >= threshold`, on integer percents.

### Semantics
- **Window instance** = (window kind, scope, `resets_at`).
  - Warn fires once per instance (dedupe key).
  - Pause fires once per instance.
  - After a resume or an override, the same instance never re-pauses. A fresh window has a new `resets_at` and re-arms.
- **Holds:**
  - A session is paused while ≥ 1 applicable hold is active.
  - Resume happens only when all of them clear.
  - Holds are stored per profile in `supervisor/<profile>.json` and applied to sessions (a model-scoped hold applies only to matching sessions).
- **Reset confirmation:** at `resets_at + 15 s` the daemon force-polls. The hold clears when the new data shows `resets_at` advanced or `percent < warn`. If it can't confirm, it retries every 30 s for up to 10 minutes, then clears based on time alone and logs a warning.
- **Stale or unknown data** (no fresh data for more than 10 min, or `status != ok`):
  - no new pauses
  - existing holds stay until a reset is confirmed, or until the time-based clear above
  - the statusline shows `?%`
- **Data freshness:** a policy evaluation runs on every new data point, either a poll or a statusline live report. Live reports update `session` and `weekly` only.

### Supervision off and manual control
- **`supervisor.enabled: false`:** polling, display, warnings (statusline and notifications), and warm-ups keep working. No holds are created and nothing is paused.
- **Manual `ccs pause`:** creates a `manual` hold with no `resume_at`. It lasts until a manual `ccs resume`.
- **Manual `ccs resume`:** clears all holds for the target and resumes the same way an automatic resume does (resume prompt if the session was interrupted mid-work). The current window instances count as resumed, so they won't re-pause.

### Poll cadence (per profile)
- 60 s by default.
- 20 s when any window is ≥ 70 % and a supervised session of the profile is busy.
- 120 s when no supervised sessions exist.
- Forced polls: at reset confirmation, after a warm-up, on app request, and on config change.
- Never more than 1 in-flight probe per profile.

## Rules for implementers
- The policy is a **pure function**: `(config, snapshot, holds, sessions, now) → (actions, new_holds, events)`. Table-driven tests cover every row above, spill on/off, warn_only on/off, stale data, and the reset-confirmation paths.
- Thresholds are never hardcoded outside `ccs.config.defaults`.
