# CC Supervisor

The macOS widgets, menu bar app, and `ccs` CLI supervise Claude Code usage across multiple profiles (`CLAUDE_CONFIG_DIR`s).

## Start here
- `docs/plans/backlog.md`: plan status. Pick work only from here.
- `docs/decisions/`: binding ADRs. Read the ones your plan links **before** coding. Deviating requires a new superseding ADR.
- `docs/manual/`: human-readable feature manual. Update the pages your plan lists and mark them `shipped`.

## Non-negotiables
- Swift = native UI/OS integration only. All logic lives in Python `ccs` (ADR-0001).
- Python runtime is stdlib only, with a pure core and thin IO shells. Tests use the fake `claude` (ADR-0013).
- No secrets stored. Auth is Claude Code's own login per config dir (ADR-0003).
- Never touch real `~/.claude*` dirs in tests. Use temp dirs and the fake `claude`.

## Workflow
- Plan lifecycle: `todo/` → `in-progress/` → `done/` via `git mv`, and update `backlog.md` in the same change (ADR-0014).
- `make test lint` must pass before a plan is done.
- Git: trunk-based on `main`, conventional commits `type(scope): desc`, no co-author or AI attribution. Commit only when the user asks.
