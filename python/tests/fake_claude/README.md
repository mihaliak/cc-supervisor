# Fake `claude`

A stand-in for the real `claude` binary so tests never touch real profiles (ADR-0013).

- **Scenario:** a JSON file whose path is in `FAKE_CLAUDE_SCENARIO`, keyed by mode (`version`, `help`, `agents`, `auth_status`, `auth_login`, `auth_logout`, `stream`, `print`, `interactive`). See the docstring in `claude` for each mode's fields.
- **Call log:** every invocation appends `{argv, env, cwd, mode}` as a JSON line to `FAKE_CLAUDE_LOG`. The env subset covers `CLAUDE*` (incl. `CLAUDE_CONFIG_DIR`, `CLAUDECODE`, `CLAUDE_CODE_*`), `CCS_*` and `AI_AGENT`.
- **`get_usage` modes** (`stream.get_usage.mode`): `ok`, `logged_out`, `malformed`, `slow`, `no_response`, `exit_early`. Hook lines are emitted unless `--settings` disables all hooks. `stream.registry: true` mimics Claude's pid registry files, which are removed only on a graceful stdin-EOF exit. `stream.ignore_sigterm` forces the SIGKILL path.
- **Auth modes:** `auth_status` (static `json`, or `stateful: true` → logged in iff `<CLAUDE_CONFIG_DIR>/.fake-auth` exists; exit 1 when logged out, like the real claude), `auth_login` (`sets_auth: true` creates the marker on exit 0), `auth_logout` (removes it). `stream.get_usage.mode: auth_marker` answers `ok` with the marker and `logged_out` without, so sign-in flows can be tested end to end.
- **`agents` mode:** `sessions` entries may use `"$PID<n>"` values, replaced by the n-th pid of `FAKE_CLAUDE_AGENTS_PIDS` (comma list), so tests can map real child pids.
- **Unknown subcommands** exit 2.
- **In tests,** use the `fake_claude(scenario)` fixture from `tests/conftest.py`. It writes the scenario, sets both env vars, and returns `(env, log_path)`.
- **Extending:** later plans add modes or fields here. Keep the file stdlib-only and backward compatible.
- **`help` mode (doctor, P13):** `claude --help` prints the captured `fixtures/claude_help/claude-2.1.281.txt` (or `help.stdout`). `help.extra_options: ["--new-flag"]` appends option lines to simulate a new Claude Code option.
- **`print` mode (warm-ups, P08):** `print.activate_window_s: N` makes later `get_usage` ok/slow payloads show an active 5h window resetting N s after the `-p` run (state in `$FAKE_CLAUDE_STATE`, default `<scenario>.state.json`).
