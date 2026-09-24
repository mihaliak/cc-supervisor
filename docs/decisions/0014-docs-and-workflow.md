# ADR-0014: Docs, plans, and git workflow

- Status: accepted
- Date: 2026-09-24
- Source: user decisions (docs structure, local git) + global git rules

## Decision

### Plans
- Files: `docs/plans/{todo,in-progress,done}/PNN-slug.md`.
- Every plan contains these sections:
  - Goal
  - Decisions (ADR links)
  - Depends on
  - Scope / Out of scope
  - Design
  - Tasks (checkboxes)
  - Tests
  - Manual pages to update
  - Done when
  - Risks
- Lifecycle:
  1. Pick the lowest-ID plan in `todo` whose dependencies are all `done`.
  2. `git mv` it to `in-progress/` and update `docs/plans/backlog.md`.
  3. Implement it, ticking tasks as you go.
  4. When every "Done when" item holds, `git mv` it to `done/`, add a `## Result` section (what shipped, deviations, follow-ups), and update the backlog.
- `docs/plans/backlog.md` is the **single status table**. It must always match the folder a plan sits in.
- Scope changes found during implementation go into a new plan (next free ID) or a follow-up line in `## Result`. Plans never silently grow.

### Decisions
- These ADRs are binding (see `docs/decisions/README.md`).
- Deviating requires a new superseding ADR first.
- Spike outcomes update the `proposed` ADRs.

### Manual
- `docs/manual/` is the human-readable description of every feature, and the source for the future README and tutorials.
- It is written as target behavior now. Each page has a `Status:` line (`planned` or `shipped`).
- A plan isn't done until the manual pages it lists describe the actual shipped behavior and are marked `shipped`.

### Git
- Local repo, trunk-based on `main`, no remote for now.
- Commits: `type(scope): description`, lowercase, imperative, subject under 50 characters, no body by default, **no co-author or AI attribution lines**.
- Commit only when the user asks.
