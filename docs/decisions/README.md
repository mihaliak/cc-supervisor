# Decisions (ADRs)

Binding project principles. Every plan and implementation must comply. If a decision needs to change, **don't edit it in place**: add a new ADR that supersedes it, then set the old one's status to `superseded by ADR-XXXX`.

- **accepted**: binding.
- **proposed**: binding default until the linked spike (P00) confirms or replaces it.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-language-split.md) | Language split: Swift = native UI only, Python = everything else | accepted |
| [0002](0002-usage-data-source.md) | Usage data source: Claude Code `get_usage` + statusline live data | accepted |
| [0003](0003-authentication.md) | Authentication: Claude Code's own login per config dir | accepted |
| [0004](0004-config-and-profiles.md) | Config file & profile model | accepted |
| [0005](0005-state-and-ipc.md) | State directory, file formats, daemon IPC | accepted |
| [0006](0006-process-model.md) | Process model: launchd daemon + PTY launcher | accepted |
| [0007](0007-pause-resume.md) | Pause/resume via PTY keystroke injection | proposed (P00-S3) |
| [0008](0008-limit-policy.md) | Limit policy & default thresholds | accepted |
| [0009](0009-display-conventions.md) | Display conventions: colors, times, statusline format | accepted |
| [0010](0010-warmup.md) | Warm-up sessions | accepted |
| [0011](0011-macos-app.md) | macOS app: menu bar agent, macOS 26, local builds, XcodeGen | accepted |
| [0012](0012-widget-data-path.md) | Widget data path & signing | proposed (P00-S1) |
| [0013](0013-python-engineering.md) | Python engineering standards | accepted |
| [0014](0014-docs-and-workflow.md) | Docs, plans, and git workflow | accepted |
| [0015](0015-notifications.md) | Notifications | accepted |
| [0016](0016-identifiers.md) | Names, identifiers, paths | accepted |
| [0017](0017-cli-surface.md) | `ccs` CLI surface | accepted |

## Template

```markdown
# ADR-XXXX: Title

- Status: accepted | proposed (<spike>) | superseded by ADR-YYYY
- Date: YYYY-MM-DD
- Source: user decision | planner default | spike result

## Context
## Decision
## Consequences
## Rules for implementers
```
