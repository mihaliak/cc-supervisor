# ADR-0002: Usage data source: Claude Code `get_usage` + statusline live data

- Status: accepted
- Date: 2026-09-24
- Source: user decision (auth question) + planner research

## Context
Three ways to get plan usage were evaluated:
1. The claude.ai web endpoint with a `sessionKey` cookie (what the third-party "Claude Usage" app does). Rejected: separate login, Cloudflare cookies expire, undocumented endpoint, secret stored in plaintext.
2. Reading Claude Code's OAuth token from Keychain and calling `/api/oauth/usage` directly. Rejected as primary: refreshing the token ourselves can rotate it and log Claude Code out.
3. **Claude Code's own `get_usage` control request.** Chosen.

Verified on 2026-09-24 with Claude Code 2.1.281:
- **Request:** `claude -p --input-format stream-json --output-format stream-json --verbose` with `CLAUDE_CONFIG_DIR` set, sending one stdin line: `{"type":"control_request","request_id":"<id>","request":{"subtype":"get_usage","skip_behaviors":true}}`
- **Response:** arrives in ~1.1 s and consumes no model tokens.
- **Payload** (`response.response.rate_limits`):
  - `five_hour` / `seven_day`: `{utilization (0-100), resets_at (ISO 8601)}`
  - `model_scoped[]`: `{display_name: "Fable", utilization, resets_at}`
  - `limits[]`: `{kind: session|weekly_all|weekly_scoped, percent, severity, resets_at, scope.model.display_name, is_active}`
  - `extra_usage`: `{is_enabled, monthly_limit, used_credits, utilization, currency, decimal_places, disabled_reason}`
  - also `subscription_type` and `rate_limits_available`
- Claude Code labels the schema **experimental**, so its shape may change.
- **Statusline stdin** also carries `rate_limits.five_hour|seven_day` `{used_percentage, resets_at (epoch s)}`. It updates after every API response, but only for the running session. It includes neither Fable nor extra usage.

## Decision
- **Primary poll source:** the daemon runs `get_usage` per profile. Every call uses `CLAUDE_CONFIG_DIR=<profile.config_dir>` and `--settings '{"disableAllHooks":true}'`, so user hooks and the statusline don't run for probes. Probes exist only to fetch usage.
- **Live supplement:** the generated statusline script writes each `rate_limits` it receives to `live/<wrapper_id>.json`. The daemon merges it: for `five_hour` and `seven_day`, the newest `observed_at` wins. This gives threshold reactions within seconds during active work.
- Normalize everything into one internal `UsageSnapshot` model (ADR-0005). No consumer reads the raw Claude payload.
- **Schema drift:** the adapter validates the fields it needs. Unknown or missing fields produce `source_error` in the snapshot plus a `usage.source_error` event. It never crashes and never shows fake zeros.

## Consequences
- Polling costs roughly one short `claude` process spawn per profile per interval (ADR-0008 sets the cadence).
- Claude Code owns token refresh, so there are no auth conflicts.
- If Claude Code removes `get_usage`, only `ccs/usage/source_claude.py` changes. The fallback (direct `/api/oauth/usage` read-only with the Keychain token, no refresh) is documented but not built unless needed.

## Rules for implementers
- Never store claude.ai cookies or OAuth tokens.
- Never call Anthropic endpoints directly while `get_usage` works.
- Keep the raw-to-`UsageSnapshot` mapping in one module with fixture-based tests. Store captured payloads under `python/tests/fixtures/get_usage/`, with personal data scrubbed.
