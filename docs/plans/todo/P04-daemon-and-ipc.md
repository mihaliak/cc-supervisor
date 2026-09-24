# P04: Daemon, IPC, events, snapshot

- Status: todo
- Milestone: M1
- Depends on: P03
- ADRs: [0002](../../decisions/0002-usage-data-source.md), [0005](../../decisions/0005-state-and-ipc.md), [0006](../../decisions/0006-process-model.md), [0008](../../decisions/0008-limit-policy.md), [0009](../../decisions/0009-display-conventions.md), [0013](../../decisions/0013-python-engineering.md), [0015](../../decisions/0015-notifications.md), [0016](../../decisions/0016-identifiers.md), [0017](../../decisions/0017-cli-surface.md)

## Goal
- The single long-running supervisor process, `ccs daemon run` under launchd. It:
  - polls usage per profile at the ADR-0008 cadence
  - merges statusline live reports
  - watches the config
  - maintains the event log and subscribers
  - writes `widget/snapshot.json`
  - serves the unix-socket IPC that launchers (P05), the policy engine (P06), warm-ups (P08), and the app (P10) plug into

## Scope
- `ccs/daemon/{__init__,server,poller,sampler,watchers,reaper,launchd,hooks,cli}.py`, `ccs/events.py`, `ccs/snapshot.py`.
- IPC proto 1 (ADR-0005):
  - Fully implemented here: `hello`, `status`, `refresh`, `subscribe`, `reload_config`.
  - Implemented with basic registry behavior here: `register_wrapper`, `unregister_wrapper`, `wrapper_event`.
  - Accepted but completed later: `pause`, `resume` (P06), `warmup` (P08). These return `{"ok": false, "error": "not_implemented"}` until then.
- CLI:
  - `ccs daemon install|uninstall|start|stop|restart|status|run|logs [--json]`
  - `ccs status [--profile <id>] [--json]`
  - `ccs events [--follow] [--json]`
- `schema/widget-snapshot.schema.json`, `schema/event.schema.json`, `schema/ipc.md` (message catalogue).

## Out of scope
- Limit policy and pause/resume logic (P06).
- Warm-up scheduling (P08).
- The PTY launcher (P05).
- Notifications posting (the app in P10; the osascript fallback in P06).

## Design

### Process and loop (`server.py`)
- `ccs daemon run`:
  1. `ensure_state_layout()`
  2. `try_lock(daemon_lock())`. If it fails, print "daemon already running" and exit 0 (launchd must not respawn-loop).
  3. Unlink a stale `daemon.sock`, set `os.umask(0o077)`, then `asyncio.start_unix_server`.
  4. Emit `daemon.started`.
  5. Run the tasks.
  6. On SIGTERM/SIGINT: graceful shutdown (emit `daemon.stopped`, close clients, remove the socket).
- `Daemon` state object:
  - `config` (last good) and `snapshots: dict[pid, UsageSnapshot]`
  - `live_reports: dict[key, LiveReport]`
  - `wrappers: dict[wrapper_id, WrapperConn]` (conn writer, profile_id, pids, cwd, registered_at)
  - `other_sessions: dict[pid, OtherSessions]`
  - `subscribers: set[Conn]`, `events: EventBus`
  - `hooks: DaemonHooks`
  - `clock: Clock`
- **Hooks (`hooks.py`):** later plans register callbacks without editing the core loop.
  - `on_usage_updated(profile_id)`
  - `on_tick(now)` every 1 s
  - `on_wrapper_registered(wrapper) -> dict` (extra reply fields, e.g. holds)
  - `on_wrapper_unregistered`, `on_wrapper_event`
  - `on_config_changed(old, new)`
  - `handle_op(op) -> handler | None` (for `pause`/`resume`/`warmup`)
  - P04 registers only the snapshot writer on `on_usage_updated` and the `on_config_changed` actions.
- Tasks:
  - `serve()`
  - one `poll_loop(profile_id)` per profile (restarted on config change)
  - `live_watch_loop()`, `config_watch_loop()`, `sampler_loop()`, `reaper_loop()`, `tick_loop()`
- Logging: a `RotatingFileHandler(logs/daemon.log, 5 MB, 3 backups)`. When `run --foreground` is used (hidden flag), also log to stderr.

### IPC (`server.py`, `schema/ipc.md`)
- JSON Lines with a 1 MB line limit.
- The first message must be `hello {proto: 1, client, version}`. A mismatched `proto` gets `{"ok": false, "error": "proto_mismatch"}` and the connection closes.
- Every request `{id, op, …}` gets exactly one response `{id, ok, …}`. Unknown op → `error: "unknown_op"`.
- Responses:
  - `status` → `{"daemon": {"version", "pid", "started_at"}, "profiles": [{"id", "usage": <snapshot dict | null>, "supervisor": <P06 fills; default {"state": "normal", "holds": []}>, "sessions": [<session record>…], "other_sessions": {"interactive": n, "background": n}, "next_warmup_at": null}]}`
  - `refresh {profile_id?}` → triggers a forced poll (all profiles if omitted) and responds `{ok: true}` immediately. The new data arrives via file and event.
  - `subscribe {topics}` → `ok`, after which the server pushes `{"event": {...}}` lines (topic `events`) and `{"snapshot": <widget snapshot>}` (topic `snapshot`) after each write.
  - `reload_config` → immediate reload, with a validation result in the reply.
  - `register_wrapper {wrapper_id, profile_id, wrapper_pid, claude_pid, cwd}` → stores the `WrapperConn` and writes `sessions/<wrapper_id>.json` with `supervision.state = "running"`, `holds = []`. It emits `session.started`, and the reply merges `hooks.on_wrapper_registered`. Re-registering the same `wrapper_id` (after a reconnect) replaces the conn and keeps the file state.
  - `unregister_wrapper {wrapper_id, exit_code}` → deletes the session file and emits `session.ended`.
  - `wrapper_event {…}` → logged and forwarded to hooks.
- Daemon → launcher commands (`{"cmd": …}`) use `WrapperConn.send_cmd(type, payload) -> Future[ack]` with a 10 s ack timeout. The API is implemented here and used by P06.
- The daemon is the **single writer** of `sessions/*.json`, `usage/*.json`, `supervisor/*.json`, `widget/snapshot.json`, and `events.jsonl`.

### Poller (`poller.py`)
- `interval_for(profile, snapshot, wrappers, activity) -> seconds` (pure, ADR-0008):
  - `fast_interval_seconds` (20) when any window percent ≥ `fast_when_percent_at_least` (70) and ≥ 1 registered wrapper of the profile has activity ≠ `idle`
  - `idle_interval_seconds` (120) when there are no registered wrappers for the profile
  - otherwise `interval_seconds` (60)
- `poll_loop`: waits for `min(interval, time to next forced poll)`, then `fetch_snapshot` → `merge` → `apply_staleness` → store → `write_snapshot` (always, even on failure) → `hooks.on_usage_updated`.
- **`usage/<profile>.json` is rewritten after every poll attempt, success or failure.** On failure, keep the last good windows, set `status`/`error`, and bump `polled_at`. The P07 statusline treats a usage file older than 5 min as `⚠ supervisor offline` (ADR-0005), so a daemon that is up but failing must still touch it.
- Forced polls come from `request_poll(profile_id, at=None) -> Awaitable[UsageSnapshot]` (resolved when the coalesced probe finishes), used by `refresh`, config change, and later P06 (reset confirmation) and P08 (post-warm-up).
- At most **one in-flight probe per profile** (asyncio.Lock). Forced requests during a probe coalesce into a single follow-up.
- Status transitions emit events:
  - to `needs_sign_in` → `auth.required` (key `auth:<pid>:<YYYYmmddHH>`)
  - to `source_error` → `usage.source_error` (key `source_error:<pid>:<hour>`)

### Watchers (`watchers.py`)
- `live_watch_loop`: every 1 s, scan the mtimes in `live/`. For changed files, `read_json` → `LiveReport`, store it, re-merge the profile's snapshot, then `write_snapshot` and `on_usage_updated` (only if a merged value changed).
  - Garbage-collect live files older than 24 h.
  - Map a report to its profile via `profile_id` in the report.
- `config_watch_loop`: every 2 s, check the config mtime and size. On change, `store.load()`:
  - valid → swap the config, call `on_config_changed`, restart poll loops for added or removed profiles
  - invalid → keep the last good config and emit `config.invalid` (key `config_invalid:<revision>`, data includes the issues)

### Sampler (`sampler.py`)
- Every 15 s for profiles with registered wrappers, and every 60 s otherwise: `agents_json(profile)`.
- For each entry with `pid` matching a registered wrapper's `claude_pid`, update the in-memory activity (`status`: busy|shell|idle|waiting) and `session_id` in the session file.
- Everything else is counted into `other_sessions` by `kind` (`interactive`, `background`). Unknown kinds count as `background`.
- **Exclude the daemon's own children.** P00-S2 showed that a running `get_usage` probe (and likewise a warm-up `claude -p`) appears in `agents --json` as `kind: interactive`, `status: idle` for its ~1.5 s lifetime. The daemon tracks the pids it spawns and skips them.
- Field names (P00-S3):
  - every entry: `pid` (only while running), `kind` (`interactive` | `background`), `startedAt` (ms), `sessionId`, `name`, `cwd`, `status` (`busy` | `idle` | `shell` | `waiting`, only while running)
  - background only: `id` (short) and `state` (e.g. `blocked`, `done`)
- Failures are logged, and the counts are kept.

### Reaper (`reaper.py`)
- Every 10 s, for each wrapper, check `os.kill(wrapper_pid, 0)`. If it's dead, treat it as `unregister_wrapper` with `exit_code = null` and emit `session.ended` with `data.reason = "reaped"`.
- At startup, remove `sessions/*.json` whose `wrapper_pid` is dead.
- Records of live wrappers are kept until they reconnect.

### Events (`events.py`)
- `Event(ts, type, profile_id, key, data)` per ADR-0005.
- `EventBus.emit(event)` is the **only** place notification fields are computed (ADR-0015), in this order:
  1. **Dedupe:** if `key` was seen within its dedupe horizon, drop it. The horizon is kept in memory and persisted in `events.seen.json` in the state dir (`paths.events_seen_file()`) (≤ 1000 keys, LRU). Hour-bucket keys give the error rate limit.
  2. **Text:** `data.title`/`data.body` = `events.notification_text(type, profile, data, now)`. P04 ships this function with a minimal fallback (title `<emoji> <name>: <type>`, empty body); P06 fills the real templates.
  3. **Notify flag:** `data.notify = type ∈ NOTIFIED_TYPES and the matching config.notifications toggle is on`. The toggle map is `limit.warn→limit_warn`, `limit.pause→limit_pause`, `limit.resume→limit_resume`, `warmup.succeeded|failed→warmup`, and `auth.required|usage.source_error|config.invalid→errors`.
  4. Append to `events.jsonl` via `append_jsonl`. Rotate at 5 MB to `events.jsonl.1` (keep 2).
  5. Push to subscribers.
- Emitters (P04, P06, P08, P09) only build `Event(type, profile_id, key, data)` and call `emit`. They never set `title`, `body`, or `notify` themselves.
- **Error-type keys carry an hour bucket** (`<YYYYmmddHH>`, local time) so they notify at most once per hour per profile (ADR-0015).

### Snapshot (`snapshot.py`, pure builder + writer)
- `level_for(percent, colors) -> "green" | "yellow" | "red"`: `< yellow_from` green, `< red_from` yellow, else red.
- `build_widget_snapshot(config, snapshots, supervisor_states, sessions, other_sessions, next_warmups, now) -> dict`:
  - `{"schema": 1, "generated_at", "profiles": [...]}`, where each profile carries:
    - `id, name, emoji, status, stale`
    - `level`: worst row level
    - `updated_at`: the latest of `fetched_at` and live `observed_at`
    - `rows`: session, weekly, then each model-scoped, then extra usage when `enabled` or `spill` is configured. Each row is `{kind, label, percent, level, resets_at, detail?}`, with labels `Session`, `Weekly`, `<name>`, `Extra usage`. The extra-usage `detail` is a precomputed amount string like `€3.20 / €10.00` (the currency symbol comes from a small ISO→symbol map, falling back to the code).
    - `supervisor`: `{state: "normal" | "warned" | "paused", active_sessions, paused_sessions, other_sessions, resume_at, next_warmup_at}`. P04 defaults these (`resume_at`/`next_warmup_at` null); P06 and P08 fill them.
  - A missing snapshot gives status `no_data` and empty rows.
- The writer is `atomic_write_json(widget_snapshot(), …)`, debounced to at most 1 write per second. It pushes `{"snapshot": …}` to subscribers.
- **`widget/snapshot.json` is rewritten after every poll attempt, success or failure, with a fresh `generated_at`.** Widgets can read only this file, so they treat `generated_at` older than 5 min as daemon offline (ADR-0005, P12).

### launchd (`launchd.py`)
- `render_plist(ccs_exec: str, env: dict, state_dir) -> bytes` via `plistlib`:
  - `Label: local.ccsupervisor.daemon`
  - `ProgramArguments: [<abs ccs path>, "daemon", "run"]`
  - `EnvironmentVariables`: `PATH`, `HOME`, plus `XDG_CONFIG_HOME`/`XDG_STATE_HOME` if set
  - `RunAtLoad: true`, `KeepAlive: true`, `ProcessType: Interactive` (avoids timer throttling)
  - `StandardOutPath`/`StandardErrorPath`: `logs/launchd.out.log` / `.err.log`
  - `ThrottleInterval: 10`
- The `ccs` path is `shutil.which("ccs")`, else `sys.argv[0]` resolved.
- Commands use `launchctl` with the domain `gui/<uid>`:
  - `install`: write the plist, `bootout` if loaded (ignore errors), then `bootstrap`
  - `uninstall`: `bootout`, then remove the plist
  - `start`: `bootstrap` if not loaded, else `kickstart`. Exposed as `launchd.start()` and `launchd.is_installed()` for the launcher's autostart (P05).
  - `stop`: `bootout`, keeping the plist
  - `restart`: `kickstart -k`
  - `status`: `launchctl print gui/<uid>/local.ccsupervisor.daemon` parsed for `state` and `pid`, plus a socket `hello` round-trip (latency) → `{"installed", "loaded", "pid", "responsive", "version", "uptime_s"}`
- `logs`: print the last 200 lines of `logs/daemon.log`.

### CLI
- `ccs status [--profile] [--json]`:
  - Daemon responsive → the `status` op.
  - Otherwise → assembled from files (`usage/*.json`, `sessions/*.json`, `supervisor/*.json`) with `daemon: {"responsive": false}`.
  - Human output: one block per profile (usage lines as in `ccs usage`, sessions count, daemon line).
- `ccs events [--follow] [--json]`:
  - Default: the last 50 lines of `events.jsonl`.
  - `--follow`: `subscribe` to `events` when the daemon is up, else tail the file with a 1 s poll.
  - Human format: `HH:MM:SS  type  profile  title/body|key`.
- Client helper `ccs/daemon/client.py`:
  - `connect(timeout=1.0) -> Client | None`
  - `request(op, **kw)`
  - `subscribe(topics) -> async iterator`
  - Shared by the CLI, the launcher (P05), and tests.

## Tasks
- [ ] `events.py` (`Event`, `EventBus.emit` pipeline: dedupe → `notification_text` (minimal fallback) → toggles → `data.notify` → append → push; dedupe persistence, rotation).
- [ ] `snapshot.py` (`level_for`, `build_widget_snapshot`, debounced writer) plus `schema/widget-snapshot.schema.json`.
- [ ] `daemon/hooks.py` (`DaemonHooks` registry with no-op defaults).
- [ ] `daemon/server.py` (lock, socket, handshake, op dispatch, `WrapperConn.send_cmd` + ack futures, graceful shutdown).
- [ ] `daemon/client.py`.
- [ ] `daemon/poller.py` (`interval_for`, `poll_loop`, `request_poll` with coalescing, status-transition events, usage-file rewrite on every attempt incl. failures with `polled_at`).
- [ ] `daemon/watchers.py` (live + config).
- [ ] `daemon/sampler.py` (activity + other sessions).
- [ ] `daemon/reaper.py`.
- [ ] `daemon/launchd.py` plus `daemon/cli.py` (`install|uninstall|start|stop|restart|status|run|logs`).
- [ ] `ccs status`, `ccs events` CLI.
- [ ] `schema/event.schema.json`, `schema/ipc.md` (every op and cmd with example request/response).
- [ ] Extend the fake claude: `agents --json` scenario (a list with a pid placeholder resolved from the env `FAKE_CLAUDE_AGENTS_PIDS`).

## Tests
- `test_ipc.py`:
  - start `Daemon` in-process on a tmp socket (FakeClock, fake claude)
  - handshake OK and proto mismatch
  - unknown op
  - `status` shape
  - `refresh` triggers exactly one probe (fake call log)
  - `subscribe` receives an emitted event and a snapshot push
  - `register_wrapper` writes the session file; `unregister_wrapper` removes it
  - `send_cmd` ack round-trip and timeout
- `test_poller.py`:
  - table for `interval_for` (all three branches plus boundaries 69/70)
  - coalescing: 3 forced requests during an in-flight probe → 1 follow-up
  - `needs_sign_in` transition emits `auth.required` once
- `test_watchers.py`: a live report file update → merged snapshot updated; invalid config → `config.invalid` and the old config kept.
- `test_sampler.py`: the agents JSON maps the claude_pid activity; others are counted by kind.
- `test_reaper.py`: a dead pid (spawned `true`) → session removed with `session.ended` `reason=reaped`.
- `test_snapshot.py`:
  - level boundaries 49/50/79/80
  - worst-level aggregation
  - row order
  - `no_data` profile
  - extra-usage `detail` formatting (EUR/USD/unknown code)
- `test_events.py`: dedupe across a restart (persisted keys), rotation at the size threshold, `data.notify` true/false per toggle, and title/body present on the appended line and in the subscriber push.
- `test_launchd.py`: `render_plist` golden file (`tests/golden/daemon.plist`). The `launchctl` calls go through an injectable runner, asserted by argv.

## Manual pages to update
- `05-ccs-cli.md`: `ccs daemon …`, `ccs status`, `ccs events`.
- `07-limits-and-supervisor.md`: the background supervisor (what runs, poll cadence).
- `11-troubleshooting.md`: daemon not running or unresponsive, logs location, `launchctl` commands, reinstall.
- `01-installation.md`: `ccs daemon install` step.

## Done when
- [ ] `make test lint` passes.
- [ ] `ccs daemon install && ccs daemon status --json` shows `loaded: true, responsive: true`.
- [ ] `usage/*.json` and `widget/snapshot.json` refresh at the configured cadence against the real profiles (observed for ≥ 5 min).
- [ ] Killing the daemon process → launchd restarts it within 15 s, and the state files survive.
- [ ] `ccs events --follow` shows `daemon.started` after a restart.
- [ ] The manual pages above are updated.

## Risks & mitigations
- **Probe spawn cost with many profiles:** the per-profile lock plus the ADR-0008 cadence. `probe_usage` durations are measured in the logs, and `ccs doctor` (P13) reports p95.
- **launchd PATH differs from the shell:** capture PATH at install. `ccs doctor` verifies `claude` resolves under the daemon's env.
- **A socket left behind after a crash:** unlink after acquiring the flock (the lock proves no other daemon is running).
- **Snapshot write storms from live reports:** 1 s debounce, and write only on changed merged values.
