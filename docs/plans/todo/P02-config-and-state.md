# P02: Config & state foundation

- Status: todo
- Milestone: M1
- Depends on: P01
- ADRs: [0004](../../decisions/0004-config-and-profiles.md), [0005](../../decisions/0005-state-and-ipc.md), [0009](../../decisions/0009-display-conventions.md), [0013](../../decisions/0013-python-engineering.md), [0016](../../decisions/0016-identifiers.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
- The shared foundation every other module uses: XDG paths, safe file IO, an injectable clock, the ADR-0009 time formatter, the config model with defaults, validation, and a revision-safe store.
- The `ccs config …` and `ccs profile …` CLIs.

## Scope
- `ccs/paths.py`, `ccs/fsio.py`, `ccs/clock.py`, `ccs/timefmt.py`
- `ccs/config/{__init__,models,defaults,validate,store,seed}.py`
- `schema/config.schema.json`, `schema/fixtures/time_format.json`
- CLI: `ccs config path|show|validate|defaults|set [--json]`, `ccs profile list|show|add|remove|set [--json]`

## Out of scope
- Consumers: daemon config watcher (P04), launcher profile resolution (P05), Swift Codable models (P11).
- The reserved-flag refresh from `claude --help` (P13 `ccs doctor`). This plan ships the static list.

## Design

### paths.py
- `config_dir() -> Path`: `$XDG_CONFIG_HOME/ccs` or `~/.config/ccs`.
- `config_file()`: `config_dir()/config.json`.
- `state_dir() -> Path`: `$XDG_STATE_HOME/ccs` or `~/.local/state/ccs`. `CCS_STATE_DIR` overrides it (set by the launcher; tests use it).
- Helpers per ADR-0005:
  - `daemon_sock()`, `daemon_lock()`, `log_dir()`
  - `usage_file(pid)`, `live_dir()`, `sessions_dir()`
  - `session_file(wid)`, `supervisor_file(pid)`, `warmup_file(pid)`, `warmup_cwd()`
  - `live_file(wrapper_id=None, session_id=None)` (`live/<wrapper_id>.json` or `live/session-<session_id>.json`), `statusline_file(pid)` (`statusline/<pid>.json`)
  - `widget_snapshot()`, `events_file()`, `events_seen_file()`, `daemon_log()`
- `ensure_state_layout()` creates the dirs (mode 0700 for the state root).
- `expand_config_dir(s: str) -> Path`: `~` expansion, then `resolve()`.

### fsio.py
- `atomic_write_json(path, obj, *, mode=0o600)`: temp file in the same dir, `json.dump(ensure_ascii=False, indent=2, sort_keys=True)` + newline (the same format the Swift writer uses in P11, so no churn), `fsync`, `os.replace`, `fsync` on the dir.
- `atomic_write_text(path, text, mode)`.
- `read_json(path) -> dict | None`: returns `None` on missing or partial or invalid files, never raises. It logs at debug level.
- `file_lock(path)`: context manager using `fcntl.flock` (`LOCK_EX`). A non-blocking variant `try_lock()` returns a bool.
- `append_jsonl(path, obj)`: `O_APPEND` single write per line.

### clock.py
- `Clock` Protocol: `now() -> datetime` (tz-aware UTC) and `monotonic() -> float`.
- `SystemClock`, and `FakeClock(start)` with `advance(seconds)`.
- `local_tz()` returns a DST-aware IANA `ZoneInfo`: `$TZ` if set, else the name from the `/etc/localtime` symlink, falling back to `datetime.now().astimezone().tzinfo`. Tests override it via `TZ` plus `time.tzset()`. P08 scheduling relies on the DST awareness.

### timefmt.py (pure, ADR-0009)
- `format_reset_absolute(reset: datetime, now: datetime, tz) -> str`:
  - same local day → `HH:MM`
  - fewer than 7 local days ahead → `Ddd HH:MM` (English abbreviations `Mon`…`Sun`)
  - otherwise → `D Mon HH:MM` (e.g. `26 Sep 08:00`)
- `format_relative(reset, now) -> str`:
  - `delta <= 0` → `now`
  - `< 60 s` → `in <1m`
  - `< 1 h` → `in {m}m`
  - `< 24 h` → `in {h}h {m}m`, dropping `{m}m` when 0
  - otherwise → `in {d}d {h}h`, dropping `{h}h` when 0
  - Round **down** to minutes.
- `format_reset_combined(reset, now, tz) -> str`: `"{abs} ({rel})"`.
- Locale-independent: no `strftime("%a")`. Use static English name tables, because Swift uses the same vectors.
- `schema/fixtures/time_format.json`: `[{ "now": ISO, "reset": ISO, "tz": "Europe/Bratislava", "absolute": "...", "relative": "...", "combined": "..." }]`. At least 20 vectors:
  - same day
  - crossing midnight (next day → `Ddd`)
  - exactly 6d 23h (→ `Ddd`), 7d (→ `D Mon`)
  - DST boundary (last Sunday of October in Europe/Bratislava)
  - 59 s → `in <1m`
  - 60 s → `in 1m`
  - 2h 0m → `in 2h`
  - 3d 0h → `in 3d`
  - past → `now`

### config/models.py
- Frozen dataclasses mirroring ADR-0004:
  - `Config`: `version, revision, default_profile, ccs_path, claude_path, display, polling, notifications, profiles, extra`
  - `Display`, `Colors`, `Polling`, `Notifications`
  - `Profile`: `id, flag, name, emoji, config_dir, limits, supervisor, statusline, warmup, extra`
  - `Limits`: `session`, `weekly` (`WarnPause`), `model_scoped` (`ModelScoped` + `warn_only`), `extra_usage` (`ExtraUsageLimits` + `spill`)
  - `SupervisorCfg`, `StatuslineCfg`
  - `WarmupCfg`: `enabled, model, prompt, triggers (Triggers: schedule[ScheduleEntry(time, weekdays)], app_start, unlock_wake, auto_chain), active_hours (start, end), cooldown_minutes`
- `extra: dict[str, Any]` on each object holds unknown keys for forward compatibility (ADR-0004 "unknown fields preserved").
- `from_dict(d) -> Config` fills missing optional fields from `defaults.py`. `to_dict(cfg) -> dict` re-emits known and extra keys.

### config/defaults.py
- The single source for every default in the ADR-0004 shape: thresholds 80/90, 80/95, 80/95/`warn_only=false`, 80/90/`spill=false`, polling 60/20/70/120, the notification toggles, warm-up `haiku` / `Reply with just: ok` / schedule `[]` / triggers on / `07:00–23:00` / cooldown 10, the resume prompt, and colors 50/80.
- `default_profile_dict(id, flag, name, emoji, config_dir)` and `default_config_dict()`.
- `ccs config defaults --json` prints `{"config": default_config_dict(), "profile": default_profile_dict(<placeholders>)}` for Swift.

### config/validate.py
- `validate(d: dict) -> list[Issue]`, where `Issue(path: str, message: str)`. It never raises.
- Rules (ADR-0004):
  - `version == 1`
  - `revision` is an int ≥ 0
  - `default_profile` references an existing id
  - `id` matches `^[a-z0-9][a-z0-9-]{0,31}$`; `id` and `flag` are unique; `flag` matches the same regex
  - `flag` is not in `RESERVED_FLAGS`
  - `config_dir` is unique after expansion and is absolute or starts with `~`
  - `emoji` is non-empty and ≤ 8 code points
  - thresholds are ints 1–100 with `warn < pause` in each pair
  - colors satisfy `0 < yellow_from < red_from <= 100`
  - `time` is `^([01]\d|2[0-3]):[0-5]\d$`; weekdays are within `mon…sun` with no duplicates
  - `cooldown_minutes` is 0–1440
  - `polling` intervals are 5–3600 s
  - `display.menu_bar` is `emoji_percent|icon_only`
  - `time_format` is `24h`
- `RESERVED_FLAGS`: `CCS_RESERVED = {help, version, profile, force, no-supervise, json}` ∪ `CLAUDE_LONG_OPTIONS`. The latter is a static frozenset of every `claude --help` long option as of 2.1.281: `add-dir, agent, agents, allow-dangerously-skip-permissions, allowedTools, allowed-tools, append-system-prompt, autocompact, ax-screen-reader, bg, background, bare, betas, brief, chrome, cloud, continue, dangerously-skip-permissions, debug, debug-file, disable-slash-commands, disallowedTools, disallowed-tools, effort, environment, exclude-dynamic-system-prompt-sections, fallback-model, file, fork-session, forward-subagent-text, from-pr, help, ide, include-hook-events, include-partial-messages, input-format, …`. Complete it from `claude --help` at implementation time, with a comment carrying the date and version.

### config/store.py
- `load() -> tuple[Config, dict]`: parsed plus raw. Missing file → `ConfigMissing`. Invalid → `ConfigInvalid(issues)`.
- `save(mutator: Callable[[dict], None], *, expected_revision: int | None = None) -> Config`. Under `file_lock(config_dir()/".config.lock")` it:
  1. reads raw
  2. raises `RevisionConflict` if `expected_revision` is given and differs
  3. applies `mutator` to the raw dict (so unknown keys are preserved)
  4. sets `revision += 1`
  5. validates, and raises `ConfigInvalid` without writing on errors
  6. `atomic_write_json`
- `ensure_config()` creates it from `seed.py` when missing and returns the loaded config.

### config/seed.py
- `seed_config() -> dict`: the defaults plus the profiles `personal` (`~/.claude`, 🏠, name `Personal`, default) and `work` (`~/.claude-work`, 💼, name `Work`).
- Only profiles whose `config_dir` exists are included. If neither exists, it seeds `personal` only.

### CLI (`ccs/cli.py` subcommands; implementation in `ccs/config/cli.py`)
- `ccs config path`: prints the path.
- `ccs config show [--json]`.
- `ccs config validate [--json]`:
  - Output `{"ok": bool, "issues": [{"path", "message"}], "revision": n}`.
  - Exit 0 when valid, 1 when not.
- `ccs config defaults --json`.
- `ccs config set <dotted.key>=<value>…`: top-level keys only (e.g. `default_profile=work`, `display.menu_bar=icon_only`, `polling.interval_seconds=90`). `profiles` is rejected (use `ccs profile …`). The same JSON-or-string value parsing, revision-safe write, and validate-before-save as `profile set`.
- `ccs profile list [--json]`: id, flag, name, emoji, config_dir, and whether it is the default.
- `ccs profile show <id> [--json]`.
- `ccs profile add --id --flag --name --emoji --config-dir [--default]`.
- `ccs profile remove <id>`: refuses if it's the default and others exist, unless `--default <other>` is given.
- `ccs profile set <id> key=value…`:
  - Dotted keys into the profile dict (e.g. `limits.session.pause=92`, `warmup.triggers.schedule='[{"time":"06:00","weekdays":["mon"]}]'`).
  - Values are `json.loads` when valid, else strings.
  - The id is immutable (error).
  - Validation errors print the issues and exit 1.
- JSON errors: `{"ok": false, "error": "...", "issues": [...]}` on stdout with exit 1. Human errors go to stderr.

## Tasks
- [ ] `paths.py` plus tests (XDG overrides, `CCS_STATE_DIR` override, `expand_config_dir`).
- [ ] `fsio.py` plus tests (atomic replace leaves no temp files; `read_json` tolerant on truncated JSON; lock exclusivity across two processes via `multiprocessing`).
- [ ] `clock.py` (`SystemClock`, `FakeClock`).
- [ ] `timefmt.py` plus `schema/fixtures/time_format.json` (≥ 20 vectors) plus a parametrized test over the fixture.
- [ ] `config/defaults.py` (all defaults in one place).
- [ ] `config/models.py` with `from_dict`/`to_dict` and extra-key preservation.
- [ ] `config/validate.py` with `RESERVED_FLAGS` (completed from `claude --help`).
- [ ] `config/store.py` with lock, revision, validate-before-write.
- [ ] `config/seed.py`.
- [ ] `schema/config.schema.json` (draft 2020-12) matching ADR-0004. A test asserts `default_config_dict()` and the seed validate against the schema's required keys. Since the runtime is stdlib-only, do a minimal structural check in the test, not a full JSON Schema validator.
- [ ] CLI wiring for `config` and `profile` subcommands in `cli.py` (argparse subparsers). `main()` routes to them.
- [ ] Docstrings on public functions. `mypy --strict` clean.

## Tests
- `test_timefmt.py`: every fixture vector (absolute, relative, combined) under the `TZ` from the vector.
- `test_validate.py`: table of an invalid config → expected issue paths:
  - duplicate id/flag/config_dir
  - reserved flag (`resume`, `help`)
  - `warn >= pause`
  - bad time/weekday
  - bad colors
  - `default_profile` missing
- `test_models_roundtrip.py`: unknown top-level, profile, and nested keys survive load → `to_dict` → save.
- `test_store.py`:
  - revision increments
  - `RevisionConflict` when `expected_revision` is stale
  - an invalid mutation leaves the file untouched
  - two processes saving concurrently both land with no lost update (the second re-reads under the lock)
- `test_seed.py`: tmp HOME with/without `.claude`/`.claude-work`.
- `test_cli_config_profile.py`:
  - `profile add/set/remove/list` with `--json` against `tmp_xdg`
  - dotted set with JSON and string values
  - exit codes

## Manual pages to update
- `10-configuration-reference.md`: every key, type, default, and validation rule (from `defaults.py` and `validate.py`).
- `05-ccs-cli.md`: `config` and `profile` commands with examples.
- `02-profiles-and-sign-in.md`: creating and editing profiles from the CLI, and reserved flag names.

## Done when
- [ ] `make test lint` passes.
- [ ] `ccs profile list --json` on a fresh `XDG_CONFIG_HOME` seeds and lists profiles.
- [ ] `ccs config validate --json` reports issues for a hand-broken file and exits 1.
- [ ] `ccs config set default_profile=work` updates the file (revision +1). `ccs config set profiles=[]` is rejected with exit 2.
- [ ] The time format vectors live in `schema/fixtures/time_format.json` (reused by P07 and P12).
- [ ] The listed manual pages describe these commands and keys.

## Risks & mitigations
- **The reserved flag list goes stale as claude adds options:** the static list is dated. `ccs doctor` (P13) diffs it against `claude --help` and warns.
- **Swift writes a config with a newer shape:** extra-key preservation plus `version` gating (reject `version > 1` with a clear message).
- **DST and timezone bugs:** fixture vectors with explicit `tz`, computed via `zoneinfo`.
