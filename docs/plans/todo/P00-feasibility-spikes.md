# P00: Feasibility spikes

- Status: todo
- Milestone: M1
- Depends on: –
- ADRs: [0002](../../decisions/0002-usage-data-source.md), [0003](../../decisions/0003-authentication.md), [0006](../../decisions/0006-process-model.md), [0007](../../decisions/0007-pause-resume.md), [0010](../../decisions/0010-warmup.md), [0012](../../decisions/0012-widget-data-path.md), [0013](../../decisions/0013-python-engineering.md)

## Goal
- De-risk the six assumptions the design rests on before any production code depends on them.
- Confirm or replace the `proposed` ADRs (0007, 0012). Fill the verified details into 0002, 0003, 0010.

## Scope
- Throwaway prototypes in `spikes/` (repo root). The folder is gitignored (P01) or deleted after the spike.
- Scrubbed fixtures that production tests reuse: `python/tests/fixtures/get_usage/`, `python/tests/fixtures/statusline/`.
- A `## Result` per spike in this file, plus proposed ADR status changes.

## Out of scope
- Production code, packaging, UI polish.
- Editing ADRs beyond a status/result note. A replaced decision gets a **new superseding ADR** (ADR-0014).

## Design
- **Safety rules for every spike:**
  - Never run `claude auth logout` on `~/.claude` or `~/.claude-work`.
  - Never modify their `settings.json`. Use `--settings` for overrides.
  - Use a temp config dir (`mktemp -d`) whenever a spike needs a logged-out or throwaway profile.
  - Spikes that consume usage (S3 busy turn, S5) require the user's explicit OK first.
  - Scrub fixtures: replace UUIDs, emails, org IDs, and `session_id` values with fixed placeholders (`00000000-0000-0000-0000-000000000000`, `user@example.com`).
- The zsh Bash tool has no `timeout`. Use Python `subprocess.run(..., timeout=…)` or `perl -e 'alarm N; exec @ARGV'` for time limits.
- **Per spike, record:** Question, Method (exact commands), Pass criteria, Fallback, Record-to, then `### Result` (date, Claude Code version from `claude --version`, observations, verdict: pass/fail/partial).

### S1: WidgetKit with ad-hoc signing (ADR-0012)
- **Question:** Does a widget extension signed "Sign to Run Locally" (ad-hoc, no Apple ID) load in the macOS 26 widget gallery? Can it read `~/.local/state/ccs/widget/snapshot.json` through `com.apple.security.temporary-exception.files.home-relative-path.read-only`? Does `WidgetCenter.shared.reloadAllTimelines()` from the host app refresh it?
- **Method:**
  1. `brew install xcodegen`.
  2. `spikes/s1-widget/project.yml` with:
     - an app target `S1Host`: SwiftUI, LSUIElement, not sandboxed, one button calling `WidgetCenter.shared.reloadAllTimelines()`
     - a widget extension `S1Widget`: sandboxed, entitlements `com.apple.security.app-sandbox=true` and the temporary exception `["/.local/state/ccs/widget/"]`
     - deployment target macOS 26.0, `CODE_SIGN_IDENTITY="-"`, `CODE_SIGN_STYLE=Manual`, no `DEVELOPMENT_TEAM`
  3. The widget renders the `updated_at` string read from the JSON file.
  4. `xcodegen generate && xcodebuild -scheme S1Host -configuration Debug -derivedDataPath build build`.
  5. Copy the app to `~/Applications/`, then `open` it.
  6. `mkdir -p ~/.local/state/ccs/widget && echo '{"updated_at":"t1"}' > ~/.local/state/ccs/widget/snapshot.json`.
  7. Add the widget via desktop right-click → Edit Widgets. Change the file to `t2`, press the reload button, and observe.
  8. Check the log: `log stream --predicate 'subsystem CONTAINS "widget" OR process CONTAINS "S1Widget"' --level debug` for sandbox denials.
- **Pass criteria:**
  - The widget appears in the gallery.
  - It renders `t1`, and `t2` shows within 10 s of the reload.
  - No sandbox denials. It still works after a logout/login.
- **Fallback A:**
  - Sign in to Xcode with a free Apple ID.
  - Use App Group `<TEAMID>.local.ccsupervisor`; the host copies the file into the group container.
  - Verify the widget loads and reads it. Note the provisioning profile expiry date (`security cms -D -i <app>/Contents/embedded.provisionprofile | grep -A1 ExpirationDate`).
- **Record-to:** this file, plus the ADR-0012 status. If fallback A wins, a new ADR supersedes 0012.

### S2: `get_usage` probe hygiene (ADR-0002)
- **Question:**
  - Does the probe with `--settings '{"disableAllHooks":true}'` run no hooks and persist no session?
  - What shapes does it return: no active window, logged out, normal?
  - What are its latency and its concurrency behavior?
- **Method** (`spikes/s2_probe.py`, stdlib):
  - Spawn `claude -p --input-format stream-json --output-format stream-json --verbose --settings '{"disableAllHooks":true}'` with `CLAUDE_CONFIG_DIR` set.
  - Write `{"type":"control_request","request_id":"s2-<n>","request":{"subtype":"get_usage","skip_behaviors":true}}\n`.
  - Read lines until a `control_response` with the matching `request_id` arrives. Record every line's `type`/`subtype`, then kill the process.
  - **Hooks:** assert no `{"type":"system","subtype":"hook_started"}` lines. Compare against the same run without `--settings`, which is expected to show hooks.
  - **Persistence:** snapshot `find <config_dir>/projects <config_dir>/sessions -type f -newer <marker>` before and after. Expect no new files. Also check `claude agents --json` doesn't list the probe.
  - **No active window:** run against the profile whose window is inactive. Check `ccs`-independent: `five_hour` utilization 0 and whether `resets_at` is null, or `five_hour: null`. If both real profiles are active, repeat later and note the time. Also record `limits[]` `kind:"session"` for that state.
  - **Logged out:** `export CLAUDE_CONFIG_DIR=$(mktemp -d)`, run the probe, and record the response or error: `control_response.subtype`, `error` text, exit code, stderr.
  - **Latency:** 20 sequential runs per real profile. Record p50/p95/max wall time to response.
  - **Concurrency:** run 3 probes in parallel while an interactive session of the same profile is busy. Expect no errors and no interference with that session.
  - **Fixtures:** save raw responses as `python/tests/fixtures/get_usage/{ok_max.json, ok_no_window.json, logged_out.json, extra_usage_enabled.json (if observable), ok_fable_scoped.json}` (scrubbed).
- **Pass criteria:**
  - No hooks, no persisted session.
  - All shapes documented, p95 < 3 s.
  - Parallel probes are safe.
- **Fallback:**
  - Hooks still run: document it and add `--setting-sources ""` if supported. Otherwise accept and note.
  - Sessions persist: add a daemon cleanup step, or cut cadence (new ADR).
  - `get_usage` missing: fall back to the documented direct `/api/oauth/usage` read-only path (new ADR).
- **Record-to:** this file, ADR-0002 (a verification note via a new ADR if the behavior differs), fixtures.

### S3: PTY proxy with the real TUI (ADR-0006, ADR-0007)
- **Question:** Can a Python PTY proxy run the `claude` TUI (user setting `"tui": "fullscreen"`) indistinguishably from a direct run? Does ESC injection interrupt a busy turn? Does bracketed paste plus `\r` submit a prompt? Does `claude agents --json` see the child?
- **Method** (`spikes/s3_pty.py`, stdlib):
  - `pty.fork()`, then the child execs `claude`.
  - The parent sets the user TTY raw (`tty.setraw`), runs a select loop copying stdin→master and master→stdout, and propagates `SIGWINCH` via `fcntl.ioctl(master, termios.TIOCSWINSZ, …)`. It restores the TTY in `finally` and exits with the child's code.
  - A control FIFO `spikes/s3.ctl` accepts the commands `esc` (write `b"\x1b"`) and `paste <text>` (write `b"\x1b[200~" + text + b"\x1b[201~"`, then `b"\r"`).
  - **Test matrix:** Ghostty and Terminal.app, with `CLAUDE_CONFIG_DIR=~/.claude-work` (a real profile, read-only use):
    - resize the window
    - mouse-wheel scroll in the transcript
    - Shift+Enter and other kitty keyboard protocol keys (Ghostty)
    - paste multi-line text
    - Ctrl-C once (interrupt) and twice (exit)
    - Ctrl-Z then `fg` (job control through the proxy)
    - `/exit`, then compare the exit code with a direct run
  - **Injection** (needs the user's OK: one small busy turn):
    - Ask for a long answer. While it streams, send `esc`. Expect generation to stop within 2 s.
    - Then send `paste Reply with just: resumed`. Expect it submitted and answered.
  - **Session map:** during the busy turn, run `CLAUDE_CONFIG_DIR=~/.claude-work claude agents --json`. Expect an entry with `pid == child pid`, `kind:"interactive"`, `status:"busy"`; after it finishes, `idle`. Record the `sessionId` field presence and name.
  - **Overhead:** time `cat` of a 50 MB file through the proxy versus direct (throughput). Subjective keystroke latency.
- **Pass criteria:**
  - No visible difference in the matrix.
  - ESC interrupts, and paste+`\r` submits.
  - The agents JSON maps the pid to status.
  - Throughput is at least 50 MB/s.
- **Fallback:** "Stop & relaunch" (ADR-0007): prototype `/exit` injection, then `claude --resume <session_id> "<prompt>"`, and verify the transcript continues. Adopt it via a superseding ADR.
- **Record-to:** this file, plus the ADR-0007 status.

### S4: Auth without a TTY (ADR-0003)
- **Question:** Does `claude auth login` complete when launched from a non-terminal parent (like the menu bar app)? What does `claude auth status --json` look like logged in and logged out?
- **Method:**
  - `D=$(mktemp -d)`.
  - `python3 -c 'import subprocess,os; e=dict(os.environ,CLAUDE_CONFIG_DIR="'$D'"); print(subprocess.run(["claude","auth","login"],stdin=subprocess.DEVNULL,capture_output=True,text=True,env=e,timeout=300))'`
  - Observe whether a browser opens, whether a method prompt blocks (claude.ai vs Console), and whether it completes after browser consent. Use a throwaway login into the temp dir only. The user performs the browser step.
  - `CLAUDE_CONFIG_DIR=$D claude auth status --json` before and after login. Then `claude auth logout` **only for `$D`**, and remove `$D`.
  - Also test `claude auth login --help` for flags (e.g. method selection) that avoid prompts.
  - Save scrubbed `python/tests/fixtures/auth_status/{logged_in.json,logged_out.json}`.
- **Pass criteria:** login completes headless (stdin DEVNULL), and the status JSON fields for logged-in/out are identified.
- **Fallback:** `ccs auth login --terminal` opens a terminal window (`open -a Terminal` with a `.command` file, or Ghostty) running `ccs auth login --profile <id>`. Record the chosen launcher.
- **Record-to:** this file, plus ADR-0003 (a note on which path is default).

### S5: Warm-up starts the window (ADR-0010)
- **Question:** Does a minimal Haiku print-mode prompt start the 5h window without persisting a session? What does it cost?
- **Method** (needs the user's OK; consumes tiny usage):
  1. Pick a profile whose window is inactive (per S2).
  2. Run the probe (before).
  3. `CLAUDE_CONFIG_DIR=<dir> claude -p "Reply with just: ok" --model haiku --no-session-persistence --settings '{"disableAllHooks":true}'` from cwd `$(mktemp -d)`.
  4. Run the probe (after) within 5 s and again after 60 s.
  5. Diff the `projects/` and `sessions/` file lists.
- **Pass criteria:**
  - `five_hour.resets_at` ≈ now + 5h (±10 min).
  - utilization delta ≤ 1%.
  - no new transcript files.
  - exit 0 within 30 s.
- **Fallback:**
  - If the window doesn't start: try the default model.
  - If files persist: accept, and let the daemon clean `warmup/cwd`-keyed project dirs (new ADR).
- **Record-to:** this file, plus ADR-0010 (the inactive-window rule 3 detail).

### S6: Statusline runtime (ADR-0006, ADR-0009)
- **Question:** What exact stdin JSON does the statusline get? Does the statusline command inherit env vars set on the `claude` process? Does `--settings '{"statusLine":…}'` override the `settings.json` statusLine? How often is it invoked?
- **Method:**
  - `spikes/s6_capture.py`: appends `{ts, env: {k:v for CCS_*/CLAUDE_CONFIG_DIR}, stdin: <json>}` to `spikes/s6.log` and prints `s6`.
  - Run `CCS_PROFILE=spike CCS_WRAPPER_ID=test CLAUDE_CONFIG_DIR=~/.claude-work claude --settings '{"statusLine":{"type":"command","command":"<abs python> <abs>/spikes/s6_capture.py"}}'`.
  - Send 2 short prompts (needs the user's OK), switch effort, `/model`, quit.
  - Save a scrubbed stdin to `python/tests/fixtures/statusline/{with_rate_limits.json, no_rate_limits_yet.json, no_effort.json}`.
  - Compute the invocation intervals from timestamps.
- **Pass criteria:**
  - Env vars present in the capture.
  - The `--settings` statusLine wins over settings.json.
  - `rate_limits.five_hour.used_percentage/resets_at` observed.
  - Invocation cadence documented.
- **Fallback:**
  - No env inheritance: bake the wrapper id into the command string (`… ccs-statusline.py --wrapper <id>`) via the per-launch `--settings`.
  - `--settings` doesn't override: the launcher must rely on `ccs statusline apply` (document it in ADR-0006 via a superseding ADR).
- **Record-to:** this file, ADR-0006/0009 notes, fixtures.

## Tasks
- [ ] Confirm with the user which spikes may consume usage (S3 injection turn, S5, S6 prompts) and when.
- [ ] `brew install xcodegen` (S1). Create `spikes/` and add it to `.git/info/exclude` if P01 hasn't landed yet.
- [ ] S1: build, test, record the Result, propose the ADR-0012 verdict.
- [ ] S2: write `spikes/s2_probe.py`, run every sub-check, save scrubbed fixtures, record the Result.
- [ ] S3: write `spikes/s3_pty.py`, run the matrix in Ghostty and Terminal.app, run injection, record the Result, propose the ADR-0007 verdict.
- [ ] S4: run the headless login in a temp dir, save the status fixtures, record the Result.
- [ ] S5: run the warm-up check, record the Result.
- [ ] S6: capture the stdin/env, save the fixtures, record the Result.
- [ ] Summarize the verdicts in a `## Summary` table (spike, verdict, ADR impact, follow-up plan changes).
- [ ] For each failed spike, draft a superseding ADR, get the user's approval, and update the affected plans' Design sections.
- [ ] Delete throwaway code (or keep it only under the gitignored `spikes/`). Fixtures stay.

## Tests
- No automated tests. The evidence is the recorded commands, outputs, and fixtures.
- The fixtures become inputs for the P03 (normalize), P07 (render), and P09 (auth status) tests.

## Manual pages to update
- `11-troubleshooting.md`: known platform limits found (e.g. widget signing expiry if fallback A, auth needing a terminal).
- `01-installation.md`: prerequisites confirmed (xcodegen, Apple ID requirement if any).

## Done when
- [ ] All six spikes have a `### Result` with a verdict.
- [ ] ADR-0007 and ADR-0012 are either confirmed (status → accepted, with a note) or superseded by new ADRs.
- [ ] Fixtures exist: `python/tests/fixtures/get_usage/` (≥ 3 shapes incl. logged-out), `python/tests/fixtures/statusline/` (≥ 2), `python/tests/fixtures/auth_status/` (2). All scrubbed.
- [ ] No real profile was logged out or had its `settings.json` modified.

## Risks & mitigations
- **Ad-hoc widget refused by WidgetKit:** fallback A, documented weekly re-sign.
- **The TUI misbehaves under the PTY (kitty keyboard, mouse):** test both terminals. If it's unfixable, "stop & relaunch" keeps the TUI native.
- **`get_usage` is experimental:** fixtures pin today's shape. The adapter isolates changes (P03).
- **Spikes consume usage:** keep prompts minimal, and do them only with the user's OK.
- **Inactive-window shape can't be observed** (profiles always active): schedule S2's sub-check after a known reset. Meanwhile, P03 treats both `null` and past `resets_at` as inactive.
