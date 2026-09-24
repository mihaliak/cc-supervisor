# P07: Statusline generate/render/apply

- Status: todo
- Milestone: M1
- Depends on: P00 (S6: statusline runtime env/stdin/`--settings` findings), P02 (paths, fsio, clock, timefmt, config), P03 (UsageSnapshot, live-report format and merge), P04 (config-change hook for script regeneration)
- ADRs: [0001](../../decisions/0001-language-split.md), [0005](../../decisions/0005-state-and-ipc.md), [0006](../../decisions/0006-process-model.md), [0009](../../decisions/0009-display-conventions.md), [0013](../../decisions/0013-python-engineering.md), [0016](../../decisions/0016-identifiers.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
Each profile gets a generated, self-contained, fast statusline script (`<config_dir>/ccs-statusline.py`):
- It renders the ADR-0009 format from Claude's stdin plus ccs state files.
- It reports live `rate_limits` back to the daemon.
- It can be applied to (and reverted from) that profile's `settings.json`.

## Scope
- `ccs/statusline/render.py`: pure segment builder (ADR-0009, exact formats).
- `ccs/statusline/runtime.py`: embeddable IO main (read stdin and state files, write the live report, print the line).
- `ccs/statusline/template.py`: generates the self-contained script.
- `ccs/statusline/apply.py`: patches/reverts `settings.json` `statusLine`.
- State file `statusline/<profile_id>.json` (apply bookkeeping).
- Writes live reports conforming to `schema/live-report.schema.json`, which P03 owns.
- CLI `ccs statusline generate|apply|revert|preview --profile <id> [--json]`.
- Daemon hook: regenerate scripts (never `settings.json`) when relevant profile/display config changes.
- `ensure_script(profile)` helper for the launcher (P05) to call before injecting `--settings`.

## Out of scope
- Launcher `--settings` injection itself (P05).
- Supervision state writes (`sessions/*.json` are written only by the daemon, P04/P06).
- Merging live reports into `usage/<profile>.json` (P03).
- Settings UI buttons (P11).
- Migrating the user's real configs (P14).

## Design
- **P00-S6 findings** (verified 2026-09-24, Claude Code 2.1.281):
  - The statusline command inherits the claude process env: `CCS_*`, `CLAUDE_EFFORT`, `COLUMNS`, `TERM`.
  - `--settings '{"statusLine":…}'` overrides the `settings.json` statusLine.
  - `rate_limits.five_hour|seven_day` = `{used_percentage: int, resets_at: int epoch seconds}`, present only after the first API response of the session.
  - `effort` is absent for Haiku and `{level}` for Sonnet/Opus.
  - Other top-level keys: `cost`, `exceeds_200k_tokens`, `fast_mode`, `scratchpad_dir`, `prompt_cache`, `prompt_id`, `session_name`.
  - **Invocation is event-driven:** at startup, after API responses, and on state changes. There is no periodic tick while idle; an 18 s gap was observed with no calls. So the 60 s `observed_at` refresh happens only when an invocation occurs.
  - Fixtures: `python/tests/fixtures/statusline/{no_rate_limits_yet,with_rate_limits,with_effort}.json`.

### Segment model
- `Segment(text: str, color: Literal["green","yellow","red","gray","plain"])`.
- `render(ctx: RenderContext) -> list[Segment]`.
- `to_ansi(segments) -> str` uses ANSI `32` / `33` / `31` / `90`, resetting after each colored segment. `to_plain(segments) -> str`.

### `RenderContext` fields
- `profile`: id, name, emoji
- `limits`: warn/pause per window, `spill`
- `colors`: `yellow_from`, `red_from`
- `now`, `stdin` (parsed dict or `{}`), `usage` (UsageSnapshot or None), `session_record` (dict or None), `wrapper_id` (str or None)

### Base line
`{emoji} {name} ~ {folder} ~ {model} / {effort} ~ {session}`
- `folder` = basename of `workspace.current_dir`, falling back to `cwd`.
- `model` = `model.display_name`.
- ` / {effort}` is dropped entirely when `effort.level` is absent. When the model is absent, drop that part too.
- An empty emoji drops the leading `emoji `.
- Separator ` ~ ` is gray.

### Session segment states
The percent and the bar carry the level color.

| State | Format |
|-------|--------|
| normal | `45% ▓▓▓▓▓░░░░░ 20:00 (in 2h 13m)` |
| warn (`percent >= limits.session.warn`, not paused or overridden) | `⚠ 84% ▓▓▓▓▓▓▓▓░░ 20:00 (in 42m) · limit approaching` |
| paused (`session_record.supervision.state == "paused"`) | `⏸ 90% ▓▓▓▓▓▓▓▓▓░ paused → resumes {abs(resume_at)} ({rel(resume_at)})`, using `supervision.resume_at`. That can be a weekly or model hold's time. |
| paused, manual hold (`resume_at` null, ADR-0008) | `⏸ 45% ▓▓▓▓▓░░░░░ paused (manual)` |
| overridden | `⚠ 91% ▓▓▓▓▓▓▓▓▓░ 20:00 (in 42m) · override` |
| no data | `?% ░░░░░░░░░░` (gray) |

### Extra segments
Appended in this order.
- ` ~ W ⚠ 86% Sat 08:00`: when `weekly >= weekly.warn` or a `weekly` hold is active. Absolute time only.
- ` ~ {name} ⚠ 82% Sat 08:00` for each `model_scoped[]` entry at or above `model_scoped.warn`, or whose `model_scoped:<name>` hold is active.
- ` ~ € 3.20/10.00`: only when spill is active (`spill` and `extra_usage.enabled`), and additionally one of:
  - `extra percent >= extra_usage.warn`
  - an `extra_usage` hold is active
  - session ≥ 100 % (running on credits)
- Currency symbols: EUR `€`, USD `$`, GBP `£`, otherwise the ISO code followed by a space.
- ` ~ ⚠ supervisor offline` (yellow) **only when the daemon is down** (ADR-0009), for wrapper and plain sessions alike.
  - The statusline never talks to the daemon (ADR-0005), so detection is file-only: offline iff `usage/<profile>.json` is missing, or its mtime is older than 5 min.
  - This depends on the daemon rewriting the file on **every** poll attempt, success or failure, at most every 120 s (P04 guarantees this).
  - A wrapper session whose `sessions/<wrapper_id>.json` is missing while the daemon is up (registration lag) shows no supervision state and no offline marker.
- **Unsupervised sessions** (no `CCS_WRAPPER_ID`, plain `claude` after `apply`): render usage, warnings, and extra segments, but never ⏸ or override (ADR-0009).

### Bar and percent
- 10 cells: `filled = round(percent / 10)` using half-up (`int(p / 10 + 0.5)`), clamped to 0..10.
- Displayed percent = `int(x + 0.5)` of stdin `used_percentage` (float) or the snapshot integer. Values over 100 are shown as-is and the bar is clamped.
- Level: `< yellow_from` green, `< red_from` yellow, otherwise red.

### Times
- `timefmt.format_reset_absolute(reset, now, tz)`, `timefmt.format_relative(reset, now)`, and `timefmt.format_reset_combined(reset, now, tz)` from P02 (ADR-0009 vectors). `tz` comes from `clock.local_tz()`.
- Combined form: `20:00 (in 2h 13m)`.

### Data precedence
- For `session` and `weekly`, stdin `rate_limits.five_hour|seven_day` wins when present:
  - `used_percentage` is a float
  - `resets_at` is epoch seconds, converted to aware UTC
- Otherwise use `usage/<profile>.json`.
- `model_scoped` and `extra_usage` always come from the usage file.

### `runtime.py` (stdlib only, embeddable)
- Parse stdin JSON (tolerate empty or invalid input: render no-data).
- Read `usage/<profile>.json` and `sessions/$CCS_WRAPPER_ID.json` (tolerant readers).
- Write the live report, then print `to_ansi(render(...))` + `\n`.
- Any exception must print a minimal fallback line (`{emoji} {name} ~ ?%`) and exit 0. Never exit non-zero, never print tracebacks.
- State dir: env `CCS_STATE_DIR` if set, otherwise the baked value.

### Live report
- Path: `live/<wrapper_id>.json`, or `live/session-<session_id>.json` when there is no wrapper id.
- Written only when stdin has `rate_limits`.
- Contents:
  ```jsonc
  {"schema":1,"profile_id","wrapper_id"|null,"session_id","model_id","effort"|null,"cwd",
   "observed_at": ISO now,
   "rate_limits":{"five_hour":{"percent","resets_at"},"seven_day":{…}}}
  ```
- Skip the write if the file already holds identical `rate_limits` + `model_id` + `effort` **and** its `observed_at` is less than 60 s old. Otherwise rewrite it with a fresh `observed_at`. Without this, a long session at a steady percent would look stale after 10 min (P03 `apply_staleness`).
- Atomic temp + `os.replace` in the same dir, no fsync (speed).

### `template.py`
- `generate(profile, config) -> GeneratedScript(path, content, generator_version, changed)`.
- Script layout:
  1. shebang `#!{sys.executable} -S -E` (convenience only; the command above is authoritative)
  2. header comment `# ccs-statusline generator={GENERATOR_VERSION} ccs={__version__} profile={id} sources={sha256[:12]}`
  3. baked constants: profile id/name/emoji, limits, colors, state dir, `GENERATOR_VERSION`
  4. `_SOURCES` dict holding the source text of `ccs.timefmt`, `ccs.statusline.render`, and `ccs.statusline.runtime`
  5. a tiny loader that creates stub packages `ccs` / `ccs.statusline` in `sys.modules`, execs the sources in dependency order, and calls `runtime.main(CONSTANTS)`
- Write atomically, mode `0755`.
- `changed=False` when the content is byte-identical.
- An import-guard test enforces that the embedded modules import only the stdlib plus each other.
- `ensure_script(profile, config) -> Path`: regenerate when the file is missing, or its header generator version or sources hash differs.
- The command used in `settings.json` and in the launcher injection is exactly `"{sys.executable} -S -E {script_path}"` (ADR-0013/0006). It does not depend on the exec bit or the shebang.

### `apply.py`
- `apply(profile) -> ApplyResult`:
  1. `ensure_script`.
  2. Read `<config_dir>/settings.json` (missing → `{}`; invalid JSON → error, no write).
  3. If `statusLine == {"type":"command","command":CMD}`, return `already_applied` (no backup, no write).
  4. Otherwise copy the file to `settings.json.ccs-backup-<YYYYmmddHHMMSS>`, record the previous `statusLine` value (or `null`) in `statusline/<profile_id>.json`, and set `statusLine`.
  5. Write with key order preserved, `indent=2`, `ensure_ascii=False`, trailing newline, atomic replace, original file mode kept.
  6. Re-read `settings.json` immediately before writing, to shrink the race with Claude Code's own writes.
- `revert(profile) -> RevertResult`:
  - `not_applied` if there is no bookkeeping file.
  - `conflict` if the current `statusLine` is no longer ours: no write, with a hint to edit manually.
  - Otherwise restore the previous value (delete the key if it was `null`) and delete the bookkeeping file.
  - The script file is **kept**, because the launcher still uses it.
- A profile with `statusline.enabled == false`:
  - `apply` → error `statusline_disabled`
  - `generate` still works
  - `ensure_script` is not called by the launcher (P05)

### Bookkeeping `statusline/<profile_id>.json`
`{"schema":1,"profile_id","config_dir","settings_path","command","previous_statusline":obj|null,"applied_at","backup_path"}`

### CLI (`cli.py` → `ccs.statusline.commands`) JSON outputs
- `generate`: `{profile_id, script_path, generator_version, changed}`
- `apply`: `{profile_id, settings_path, result: applied|already_applied, backup_path|null, command}`
- `revert`: `{profile_id, result: reverted|not_applied|conflict, restored|null}`
- `preview`: `{profile_id, status: {applied, script_path, script_current}, samples: [{state, plain, ansi, segments: [{text, color}]}]}`
  - Sample states: normal, warn, paused, overridden, no_data, weekly_warn, model_scoped_warn, spill, supervisor_offline.
  - Built from fixed fake contexts with real profile name/emoji and `now`.
- Human (non-JSON) `preview` prints the ANSI samples, one per line, prefixed by the state name.

### Daemon regeneration
- Register a callback on P04's config-change hook: for each profile whose `id`, `name`, `emoji`, `config_dir`, `limits`, `statusline.enabled`, or `display.colors` changed, call `template.generate` (if enabled).
- Errors → `logs/daemon.log` only.

## Tasks
- [ ] Validate written live reports against P03's `schema/live-report.schema.json` in tests. Add `python/tests/fixtures/statusline/stdin/*.json` samples taken from P00-S6 (scrubbed).
- [ ] `ccs/statusline/render.py`: `Segment`, `RenderContext`, `render`, `to_ansi`, `to_plain`, `level_for(percent, colors)`, `bar(percent)`, `currency_symbol(code)`.
- [ ] Session segment: all states (normal, warn, paused, paused-manual, overridden, no data), exact strings per ADR-0009 + the manual-hold row above.
- [ ] Extra segments: weekly, model-scoped (multiple), extra usage (spill rules above), supervisor offline.
- [ ] Data precedence and stdin conversion (float percent rounding, epoch → aware datetime).
- [ ] `ccs/statusline/runtime.py`: `main(constants)` with tolerant IO, live-report write-if-changed-or-older-than-60s, and the fallback line on exception.
- [ ] `ccs/statusline/template.py`: `generate`, `ensure_script`, `GENERATOR_VERSION`, `statusline_command(script_path)`, sources hash, loader stub.
- [ ] Import-guard test: `timefmt`, `render`, and `runtime` import only the stdlib plus each other.
- [ ] `ccs/statusline/apply.py`: `apply`, `revert`, bookkeeping read/write, backup naming, invalid-JSON guard.
- [ ] CLI subcommands `generate|apply|revert|preview` with `--profile` (required) and `--json`; exit codes 0/1/2.
- [ ] Daemon config-change hook → regenerate scripts (P04 callback API).
- [ ] Export `ensure_script` + `statusline_command` for P05, and document the call site in P05's plan file (Result section) if P05 is already done.
- [ ] Perf harness `python/tests/statusline/test_perf.py` (marked `perf`, run by `make test`): 50 executions of the generated script with sample stdin; assert p95 < 60 ms.

## Tests
- **Golden render table** (`test_render.py`), parametrized: every state × {effort present/absent, model absent, emoji empty} × percents {0, 5, 45, 50, 79, 80, 84, 90, 100, 117} × {stdin only, file only, both (stdin wins), neither}.
  - Asserts the exact plain string and the per-segment colors.
- Extra segment visibility matrix: weekly/model-scoped at warn−1, warn, and hold active; spill on/off × credits enabled/disabled × extra % × session ≥ 100.
- Supervisor offline: usage file missing / mtime > 5 min → offline (with and without a wrapper id); wrapper id + missing record + fresh usage → no marker and no supervision state; plain session never shows ⏸/override.
- Time strings via the P02 fixtures (`schema/fixtures/time_format.json`) at a fixed `now`.
- Generated script executed in a subprocess with sample stdin and a temp state dir (`CCS_STATE_DIR`):
  - output matches `render` plain after stripping ANSI
  - the live report is written once, not rewritten for identical stdin within 60 s, and rewritten (fresh `observed_at`) after 60 s
- Robustness: garbage stdin, empty stdin, unreadable state files → fallback line, exit 0.
- `apply` / `revert` on temp config dirs:
  - fresh (no `settings.json`)
  - existing with another `statusLine` (the value from P14: `bash …/statusline-command.sh`)
  - idempotent second apply
  - revert restores the exact previous value
  - revert conflict
  - invalid JSON untouched
  - other keys and their order preserved
  - file mode preserved
- Perf: p95 < 60 ms (see Tasks).

## Manual pages to update
- `06-statusline.md` (format, states, apply/revert, preview)
- `05-ccs-cli.md` (`ccs statusline …`)
- `10-configuration-reference.md` (`statusline.enabled`, `display.colors`)
- `11-troubleshooting.md` (script stale, offline indicator, revert conflict)

## Done when
- [ ] Every render state matches ADR-0009 strings exactly (golden tests green).
- [ ] The generated script runs standalone via `<abs python> -S -E <script>`, uses the stdlib only, and has p95 < 60 ms.
- [ ] Apply/revert are idempotent, keep backups, and never clobber invalid or foreign `settings.json` content.
- [ ] The daemon regenerates scripts on relevant config changes.
- [ ] `make test lint` passes.
- [ ] Listed manual pages describe the shipped behavior and are marked `shipped`.

## Risks & mitigations
- **Claude Code changes the stdin schema** → tolerant parsing, the no-data fallback, and fixtures refreshed from P00-S6 / `ccs doctor`.
- **The pipx venv Python disappears** (pyenv upgrade) → the command breaks. `ccs doctor` (P13) checks that the interpreter exists and suggests `ccs statusline generate`. `make upgrade` regenerates.
- **Claude Code rewrites `settings.json` concurrently** → re-read just before the write, atomic replace, backup kept.
- **Embedded-source loader fragility** → the import-guard test plus the subprocess execution test in CI.
- **Live-report write cost on every refresh** → write-if-changed, no fsync.
