# Backlog

The single source of truth for plan status. Workflow rules are in [ADR-0014](../decisions/0014-docs-and-workflow.md):
- Pick the lowest-ID `todo` plan whose dependencies are all `done`.
- `git mv` it between `todo/` → `in-progress/` → `done/`.
- Update this table in the same change.
- A plan is done only when its "Done when" list holds, `make test lint` passes, and its manual pages are marked `shipped`.

## Milestones
- **M1: terminal supervision** (P00–P09). `ccs --work` runs claude under supervision with the statusline, warn/pause/resume, and warm-ups. No GUI.
- **M2: native macOS** (P10–P12). Menu bar app, settings, sign-in, notifications, widgets.
- **M3: install & migration** (P13–P14). One-command install, doctor, move off the old statusline and the Claude Usage app.

## Plans
| ID | Plan | Status | Depends on | Area |
|----|------|--------|------------|------|
| P00 | [Feasibility spikes](in-progress/P00-feasibility-spikes.md) | in-progress | – | research |
| P01 | [Repo scaffolding & tooling](done/P01-repo-scaffolding.md) | done | – | infra |
| P02 | [Config & state foundation](todo/P02-config-and-state.md) | todo | P01 | python |
| P03 | [Usage fetching & normalization](todo/P03-usage-fetching.md) | todo | P00, P02 | python |
| P04 | [Daemon, IPC, events, snapshot](todo/P04-daemon-and-ipc.md) | todo | P03 | python |
| P05 | [ccs launcher & PTY proxy](todo/P05-ccs-launcher-pty.md) | todo | P00, P04 | python |
| P06 | [Supervisor policy & pause/resume](todo/P06-supervisor-policy.md) | todo | P04, P05 | python |
| P07 | [Statusline generate/render/apply](todo/P07-statusline.md) | todo | P00, P02, P03, P04 | python |
| P08 | [Warm-up scheduler](todo/P08-warmup.md) | todo | P00, P04 | python |
| P09 | [Auth wrapper](todo/P09-auth.md) | todo | P00, P02, P03, P04 | python |
| P10 | [macOS app shell (menu bar, bridge, notifications, OS triggers)](todo/P10-macos-app-shell.md) | todo | P00, P04 | swift |
| P11 | [Settings UI (profiles, sign-in, statusline apply)](todo/P11-settings-ui.md) | todo | P07, P09, P10 | swift |
| P12 | [Widgets (small/medium/large)](todo/P12-widgets.md) | todo | P00, P06, P10 | swift |
| P13 | [Install, uninstall, doctor](todo/P13-install-and-doctor.md) | todo | P06, P07, P08, P09, P12 | infra |
| P14 | [Migration from current setup](todo/P14-migration.md) | todo | P11, P13 | ops |

## Parallelism hints
- P00 and P01 can run in parallel.
- After P04: P05, P07, P08, P09 are independent (P06 needs P05).
- P10 can start once P04 is done, in parallel with P05–P09.
