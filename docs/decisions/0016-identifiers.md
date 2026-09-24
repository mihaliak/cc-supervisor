# ADR-0016: Names, identifiers, paths

- Status: accepted
- Date: 2026-09-24
- Source: planner default (the `local.` reverse-DNS prefix avoids needing an owned domain for personal builds; it can be changed before any distribution)

## Decision
| Thing | Value |
|-------|-------|
| Product | CC Supervisor (repo `cc-supervisor`) |
| CLI | `ccs` |
| Python dist / package | `cc-supervisor` / `ccs` |
| App bundle | `CC Supervisor.app`, id `local.ccsupervisor.app` |
| Widget extension | `local.ccsupervisor.app.widgets` |
| URL scheme | `ccsupervisor://` |
| LaunchAgent | label `local.ccsupervisor.daemon`, file `~/Library/LaunchAgents/local.ccsupervisor.daemon.plist` |
| Config | `~/.config/ccs/config.json` (`$XDG_CONFIG_HOME`) |
| State | `~/.local/state/ccs/` (`$XDG_STATE_HOME`) |
| Statusline script | `<config_dir>/ccs-statusline.py` |
| settings.json backup | `<config_dir>/settings.json.ccs-backup-<YYYYmmddHHMMSS>` |
| Env for claude child | `CLAUDE_CONFIG_DIR`, `CCS_PROFILE`, `CCS_WRAPPER_ID`, `CCS_STATE_DIR` |

## Repo layout
```
python/                 pyproject.toml, src/ccs/, tests/ (incl. fake_claude/)
macos/                  project.yml, App/, Widgets/, Shared/, Tests/
schema/                 JSON Schemas + shared fixtures (time_format.json, snapshots)
docs/                   plans/, decisions/, manual/
Makefile                venv, install-dev, test, test-live, lint, fmt, xcodegen-check, project, app, clean (P01); install, upgrade, uninstall (P13)
CLAUDE.md               agent entry point (workflow + ADR pointers)
```

### Python package map (`python/src/ccs/`)
```
cli.py            argparse + launcher pre-parse
paths.py          XDG paths, state layout
fsio.py           atomic write, locks, tolerant JSON read
clock.py          injectable clock
timefmt.py        ADR-0009 time formatting (pure)
config/           models.py, defaults.py, validate.py, store.py (revision-safe IO), seed.py
claude_cli.py     locate claude; run agents --json, auth status/login, -p; get_usage probe
usage/            normalize.py (raw → UsageSnapshot, pure), source_claude.py, merge.py (poll + live)
daemon/           server.py (asyncio, socket), poller.py, scheduler.py, reaper.py, launchd.py
supervisor/       policy.py (pure), engine.py (holds, commands), ledger.py
launcher/         pty_proxy.py, session_map.py, inject.py, prompt.py
statusline/       render.py (pure), runtime.py (stdin/IO part embedded in the script), template.py (generated script), apply.py (settings.json patch)
warmup/           rules.py (pure), runner.py
snapshot.py       builds widget/snapshot.json (pure builder + writer)
events.py         event model, dedupe, jsonl writer
notify.py         osascript fallback
auth.py           ccs auth login/status/logout wrapper
doctor.py         diagnostics
```
