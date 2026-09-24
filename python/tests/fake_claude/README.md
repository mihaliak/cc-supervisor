# Fake `claude`

A stand-in for the real `claude` binary so tests never touch real profiles (ADR-0013).

- **Scenario:** a JSON file whose path is in `FAKE_CLAUDE_SCENARIO`, keyed by mode (`version`, `agents`, `auth_status`, `auth_login`, `stream`, `print`, `interactive`). See the docstring in `claude` for each mode's fields.
- **Call log:** every invocation appends `{argv, env, cwd, mode}` as a JSON line to `FAKE_CLAUDE_LOG`. The env subset covers `CLAUDE*` (incl. `CLAUDE_CONFIG_DIR`, `CLAUDECODE`, `CLAUDE_CODE_*`), `CCS_*` and `AI_AGENT`.
- **`get_usage` modes** (`stream.get_usage.mode`): `ok`, `logged_out`, `malformed`, `slow`, `no_response`, `exit_early`. Hook lines are emitted unless `--settings` disables all hooks. `stream.registry: true` mimics Claude's pid registry files, which are removed only on a graceful stdin-EOF exit. `stream.ignore_sigterm` forces the SIGKILL path.
- **`agents` mode:** `sessions` entries may use `"$PID<n>"` values, replaced by the n-th pid of `FAKE_CLAUDE_AGENTS_PIDS` (comma list), so tests can map real child pids.
- **Unknown subcommands** exit 2.
- **In tests,** use the `fake_claude(scenario)` fixture from `tests/conftest.py`. It writes the scenario, sets both env vars, and returns `(env, log_path)`.
- **Extending:** later plans add modes or fields here. Keep the file stdlib-only and backward compatible.
