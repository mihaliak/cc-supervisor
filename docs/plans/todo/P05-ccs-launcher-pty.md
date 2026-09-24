# P05: ccs launcher & PTY proxy

- Status: todo
- Milestone: M1
- Depends on: P00 (S3, S6 verdicts), P04
- ADRs: [0006](../../decisions/0006-process-model.md), [0007](../../decisions/0007-pause-resume.md), [0005](../../decisions/0005-state-and-ipc.md), [0013](../../decisions/0013-python-engineering.md), [0016](../../decisions/0016-identifiers.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
- `ccs --work [claude args…]` opens the normal interactive Claude Code TUI in the current terminal, byte-for-byte transparent.
- It sets the profile env, injects the statusline, registers with the daemon, and executes the daemon's pause/resume commands via keystroke injection. Overrides are detected.

## Scope
- `ccs/cli.py` launcher pre-parse and routing.
- `ccs/launcher/{__init__,main,pty_proxy,inject,session_map,prompt,daemon_link}.py`.
- Command handling for the `pause`/`resume` cmds, as the launcher side of ADR-0007.
- A fake claude interactive TUI mode for tests.

## Out of scope
- Deciding **when** to pause or resume, holds, and `was_busy_at_pause` bookkeeping (daemon side, P06).
- Statusline script generation (P07). The launcher only calls its `ensure` hook.
- Background agents and plain `claude` sessions (ADR-0007 scope).

## Design

### Pre-parse (`cli.py: parse_launcher_args(argv, config) -> LaunchSpec | None`)
- `argv[0]` is a known subcommand (`status`, `usage`, `sessions`, `events`, `pause`, `resume`, `warmup`, `auth`, `statusline`, `profile`, `config`, `daemon`, `doctor`) or `--version`/`-h`/`--help` → return `None`, and argparse handles it.
- Otherwise, consume ccs-only options from the **front** of argv, in any order, until the first token that isn't one (with or without a profile flag):
  - `--profile <id>` → that profile.
  - `--<flag>` matching a profile flag → that profile. A second profile selector is an error (exit 2).
  - `--force`, `--no-supervise`.
- Everything from the first other token on is claude args. With no profile selector, use `default_profile`.
- An unknown `--<x>` that isn't a profile flag ends the ccs options and passes to claude (e.g. `ccs --resume`). The exception is a likely typo of a profile flag (edit distance ≤ 2 from a flag, and not a known claude option): error `unknown profile '--wrok'. Did you mean --work?` (exit 2).
- `LaunchSpec(profile, force, no_supervise, claude_args)`.

### Mode selection (`launcher/main.py: run(spec) -> int`)
- **Passthrough exec, no PTY, no supervision:**
  - When `claude_args` contains `-p`/`--print`, **or** stdin or stdout is not a TTY.
  - `os.execvpe(claude, [claude, *args], env)` with the profile env. Print runs are short and scriptable; a PTY would break pipes.
- **Otherwise supervised interactive:**
  1. Resolve `claude` (`claude_cli.resolve_claude`).
  2. Build the env: `CLAUDE_CONFIG_DIR=<abs>`, `CCS_PROFILE=<id>`, `CCS_WRAPPER_ID=<uuid4>`, `CCS_STATE_DIR=<abs state dir>`.
  3. Statusline injection (ADR-0006, ADR-0013):
     - If `profile.statusline.enabled` and `--settings` is not in `claude_args`, call `ensure_statusline(profile) -> str | None`. This hook is implemented by P07 as `ccs.statusline.apply.ensure_script`. Until P07 lands, return the command only if the script file exists.
     - On success, prepend `--settings '{"statusLine":{"type":"command","command":"<cmd>"}}'`.
     - If the user passed `--settings`, don't inject. Print one line to stderr before starting: `ccs: --settings given; statusline not injected (run: ccs statusline apply --profile <id>)`.
  4. The daemon link (below), unless `--no-supervise`.
  5. Start-while-held check (below).
  6. PTY proxy.
  7. Unregister.
  8. Return claude's exit code.

### Daemon link (`launcher/daemon_link.py`)
- `connect()`:
  - `client.connect(timeout=1)`.
  - On failure: if `launchd.is_installed()`, start it with P04's `ccs daemon start` logic (`launchd.start()`: `bootstrap` if the job is unloaded, else `kickstart gui/<uid>/local.ccsupervisor.daemon`) and retry 3× at 500 ms.
  - If not installed: stderr `ccs: supervisor not installed — running unsupervised (ccs daemon install)` → unsupervised.
  - Installed but still unreachable: stderr `ccs: supervisor not responding — running unsupervised`, then keep retrying in the background.
- Before spawning claude, `request("status", profile_id=…)` reads the active holds for the profile.
- After spawning:
  - `register_wrapper {wrapper_id, profile_id, wrapper_pid, claude_pid, cwd, started_overridden}`. `started_overridden` is true when the user answered `y` at the held prompt (an additive field; the daemon side is P06).
  - The reply may include `holds` and `supervision`, which set the local `paused` state.
- **Reconnect:** on EOF or error, back off 0.5/1/2/4/8/10 s (capped), then re-`hello` and re-`register_wrapper` with the **same** `wrapper_id`. The daemon (P04) keeps the file state across re-registration.
- **Incoming `{"cmd": …}`** is dispatched to the command handler, which acks `{"ack": cmd_id, "result", "detail"}`.
- On exit: `unregister_wrapper {wrapper_id, exit_code}` (best effort, 1 s timeout).

### Start-while-held (`launcher/prompt.py`)
- If the status shows **any** active hold applying to the profile (`session`, `weekly`, `extra_usage`, `manual`, or a `model_scoped:<name>` hold; the model isn't known yet at start, so any model-scoped hold asks too) and `--force` is not set:
  - Print `Profile <name> is paused until HH:MM (in 42m). Start anyway? [y/N] ` (`timefmt`), using the latest `resume_at` among the holds.
  - If no active hold has a `resume_at` (a manual hold only), print `Profile <name> is paused (manual). Start anyway? [y/N] `.
  - Read one line in cooked mode.
  - `y`/`yes` → continue with `started_overridden = true`. Anything else → exit 0 without starting.
- `--force` → continue with `started_overridden = true` silently.

### PTY proxy (`launcher/pty_proxy.py`)
- Capture the user TTY winsize (`TIOCGWINSZ` on stdin), then `pid, master = pty.fork()`. In the child: set the winsize on fd 0, then `os.execvpe(claude, argv, env)`. On exec failure, write the error and `os._exit(127)`.
- Parent, asyncio loop:
  - Save `termios` attrs, then `tty.setraw(stdin)`.
  - `loop.add_reader(stdin)`: read → `on_user_input(bytes)` → buffered write to master (`add_writer` for partial writes).
  - `loop.add_reader(master)`: read → write to stdout (blocking `os.write` loop). On `OSError EIO`, the child closed.
  - Signal handlers:
    - `SIGWINCH` → `TIOCGWINSZ` stdin → `TIOCSWINSZ` master. The kernel signals the child's foreground process group.
    - `SIGTERM`/`SIGHUP` → forward to the child, wait up to 5 s, then `SIGKILL`.
    - `SIGINT` never arrives from the keyboard in raw mode. If received externally, forward it.
    - `SIGCHLD` → `waitpid(pid, WNOHANG | WUNTRACED)`:
      - **stopped child** (defensive only): restore termios, `os.kill(os.getpid(), SIGSTOP)`. On resume (`SIGCONT`), re-raw and `os.kill(pid, SIGCONT)`.
  - **Ctrl-Z is intercepted by the proxy** (P00-S3 finding). Under `pty.fork` claude is a session leader in an orphaned process group, so its own Ctrl-Z suspend is silently discarded: it prints "Claude Code has been suspended" but keeps running, and the user's shell never regains control. So:
    1. Strip `\x1a`, `\x1b[122;5u` and `\x1b[122;5:1u` (kitty keyboard encodings) from user input.
    2. Restore termios and `os.kill(os.getpid(), SIGTSTP)`, so the shell sees the job stop.
    3. On `SIGCONT`, re-raw and force a redraw by nudging the winsize (rows-1, then back, 50 ms apart) so the fullscreen TUI repaints.
    - Verified in S3 with the spike proxy: proxy `T` while claude kept running, and `fg` restored it.
      - **exited child:** finish.
  - `finally` (all paths, including exceptions): restore termios `TCSAFLUSH`, remove readers, close master.
- `exit_code = os.waitstatus_to_exitcode(status)`. For signals it is `128 + signum`.
- The proxy exposes to the command handler:
  - `write_to_child(bytes)`
  - `last_user_input_at` (monotonic)
  - `claude_pid`
  - `on_user_submit` callback, fired when user input contains `\r` or `\n`

### Injection (`launcher/inject.py`, pure builders + async guard)
- `ESC = b"\x1b"`.
- `paste(text) = b"\x1b[200~" + text.encode() + b"\x1b[201~"`, then `SUBMIT = b"\r"`, written as a separate write 50 ms later.
- `async wait_for_typing_gap(proxy, gap=1.5, max_wait=30)`: waits until `now - last_user_input_at ≥ gap`, or until `max_wait` elapses, then proceeds anyway (ADR-0007).
- Every injection is reported as `wrapper_event {kind: "injected"|"inject_failed", detail: {cmd_id, what: "esc"|"resume_prompt"}}`.

### Session map (`launcher/session_map.py`)
- `async lookup(claude, profile, claude_pid) -> SessionInfo(session_id: str | None, status: "busy" | "shell" | "idle" | "waiting" | "unknown")` via `claude_cli.agents_json`. Fields verified in P00-S3: the entry has `pid`, `sessionId`, `status`, `kind: "interactive"`, `name`, `startedAt`, `cwd`. It became `busy` within 0.5 s of submit and `idle` after ESC.
- **Never inject slash commands** (e.g. `/model`). They can persist user settings: in P00-S3 `/model sonnet` rewrote `~/.claude/settings.json` `model`. Only ESC and the resume prompt text are ever injected.
- It matches the entry with `pid == claude_pid` (the field names are confirmed by P00-S3). Not found → `unknown`.
- `is_busy(status)`: `status != "idle"`. Per ADR-0007, anything other than `idle` counts as busy, including `unknown`, so ESC is sent.

### Command handler (launcher side of ADR-0007)
- `pause {cmd_id, holds, resume_at}`:
  1. Set local `paused = True` and `override_reported = False`.
  2. `info = lookup()`.
  3. If busy: `wait_for_typing_gap`, write `ESC`, sleep 2 s, `lookup()` again. If still busy, write `ESC` once more. Ack `injected`, `detail {was_busy: true, interrupted: <final status not busy>, session_id}`.
  4. If not busy: ack `skipped`, `detail {was_busy: false, session_id}`.
- `resume {cmd_id, prompt: str | null}`:
  1. Set `paused = False`.
  2. If `prompt` is null, ack `skipped`.
  3. Otherwise, poll `lookup()` every 2 s for up to 30 s until not busy. Then `wait_for_typing_gap`, write `paste(prompt)`, then `SUBMIT`. Ack `injected`.
  4. If still busy after 30 s, ack `skipped`, `detail {reason: "busy"}`.
- **Override detection:** while `paused` and `not override_reported`, user input containing `\r` sends `wrapper_event {kind: "input_submitted_while_paused"}` once and sets `override_reported`. Input always passes through (ADR-0007).
- Unknown `cmd.type` → ack `failed`, `detail {reason: "unknown_cmd"}`.

### Fake claude TUI mode (tests)
- Default mode (no `-p`, no subcommand):
  - Puts its TTY in raw mode and appends every read chunk (hex) to `$FAKE_CLAUDE_LOG`.
  - Prints `fake> `.
  - On `SIGWINCH`, logs `{"winsize": [rows, cols]}`.
  - `status` behavior for `agents --json` is driven by the scenario (e.g. `busy` until an ESC byte is received, then `idle`), with a shared state file.
  - Exits with the `tui.exit_code` scenario value when it receives `\x04` (Ctrl-D).
  - Supports `tui.stop_on` (`\x1a` → `os.kill(os.getpid(), SIGTSTP)`) to test job control.

## Tasks
- [ ] `parse_launcher_args` plus typo suggestion; wire into `cli.main` before argparse.
- [ ] `launcher/main.py`: mode selection, env build, statusline injection hook, `--no-supervise`.
- [ ] `launcher/pty_proxy.py`: fork/exec, raw mode, readers/writers, winsize, signals, job control, exit code, guaranteed restore.
- [ ] `launcher/inject.py`: byte builders, typing guard.
- [ ] `launcher/session_map.py`.
- [ ] `launcher/daemon_link.py`: connect/autostart (reuse `launchd.start()`)/unsupervised, status pre-check, register/reconnect/unregister, cmd dispatch and acks.
- [ ] `launcher/prompt.py`: held prompt, `--force`.
- [ ] Command handler (`pause`/`resume`) plus override detection.
- [ ] Fake claude TUI mode, the agents-status state machine, and SIGWINCH/exit code/SIGTSTP support.
- [ ] Update P00-S3 findings into code comments where the behavior depends on them (agents JSON field names, ESC semantics).

## Tests
- `test_launcher_parse.py`, table:
  - `[]` → default profile
  - `["--work"]`
  - `["--work", "--force", "-c"]` → force, args `["-c"]`
  - `["--force"]` → default profile, force, args `[]`
  - `["--no-supervise", "-c"]` → default profile, no_supervise, args `["-c"]`
  - `["--force", "--work"]` → work, force (any order)
  - `["--work", "--personal"]` → error (two profiles, exit 2)
  - `["--profile", "work", "--resume", "abc"]`
  - `["--resume"]` → default profile with claude args
  - `["--wrok"]` → typo error
  - `["status"]` → None
  - `["--version"]` → None
- `test_pty_proxy.py`: the test runs `ccs --work` under its own `pty.fork()` with the fake claude and an in-process daemon (P04 test server).
  - bytes typed into the test master arrive unchanged in the fake log (incl. `\x1b[200~…`, `\x03`, UTF-8 multibyte)
  - child output arrives unchanged
  - resize the test master plus `SIGWINCH` → fake logs the new winsize
  - Ctrl-D → the launcher exits with the scenario exit code
  - the TTY attrs are restored after exit (compare `tcgetattr` before and after)
- `test_injection.py`:
  - daemon sends `pause` while fake status is `busy` → fake log contains `1b`, and the ack is `injected, was_busy=true`
  - `pause` while `idle` → no ESC, ack `skipped`
  - `resume` with a prompt → the log contains the bracketed paste plus `0d`
  - user typing right before a resume → the injection is delayed by ≥ 1.5 s
  - user `\r` while paused → a single `input_submitted_while_paused` event
- `test_daemon_link.py`:
  - no daemon, not installed → runs unsupervised with the stderr hint
  - installed but stopped (booted out) → the launcher bootstraps it, then registers
  - daemon killed mid-session → the launcher re-registers the same `wrapper_id` after restart
  - held status plus answer `n` → claude never spawned (fake call log empty)
  - manual hold only → the prompt reads `paused (manual)`
  - `pause` while fake status is `unknown` → ESC injected (counts as busy)
  - `--force` → spawned with `started_overridden`
- `test_passthrough.py`: `ccs --work -p hi` with stdout piped → exec without a PTY, env contains `CLAUDE_CONFIG_DIR`.

## Manual pages to update
- `05-ccs-cli.md`: launcher syntax, profile selection, `--force`, `--no-supervise`, argument passthrough, print-mode behavior.
- `07-limits-and-supervisor.md`: what pausing looks like in the terminal (ESC interrupt, statusline ⏸, auto-resume prompt, typing overrides).
- `02-profiles-and-sign-in.md`: `ccs --<flag>` per profile, default profile.
- `11-troubleshooting.md`: "supervisor offline" message, terminal left in a bad state (`reset`), `--no-supervise` for debugging.

## Done when
- [ ] `make test lint` passes.
- [ ] Manual check in Ghostty and Terminal.app: `ccs --work` is indistinguishable from `CLAUDE_CONFIG_DIR=~/.claude-work claude` across the P00-S3 matrix (resize, scroll, paste, Ctrl-C, Ctrl-Z/fg, exit code).
- [ ] Manual check: `ccs pause --profile work`/`ccs resume --profile work` (available after P06; before that via a test daemon command) interrupts a busy turn and later submits the resume prompt.
- [ ] The terminal is always restored, including after `kill -TERM <launcher pid>`.
- [ ] The manual pages above are updated.

## Risks & mitigations
- **The TUI uses terminal features that break through a PTY:** P00-S3 gate. Fallback "stop & relaunch" per ADR-0007 (a superseding ADR if adopted).
- **ESC is ambiguous as the start of an escape sequence**, so claude may wait for more bytes: send a lone ESC, then verify via `lookup()` and retry once. If S3 shows it needs a double ESC, adjust `inject.py`.
- **Injecting while the user types corrupts their input:** typing guard, and injection only on explicit daemon commands.
- **Job control (Ctrl-Z) leaves the shell stuck:** the explicit stopped-child handling plus a test.
- **The `claude agents --json` spawn is slow:** only called on pause/resume, not in the hot path.
