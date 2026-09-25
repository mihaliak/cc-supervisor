#!/usr/bin/env bash
# `make upgrade`, after `install-dev` (pipx reinstall) and `app` ran (P13): reinstall the daemon's
# LaunchAgent (so it gets the new plist, e.g. `run --launchd` and the `CCS_STATE_DIR` passthrough)
# and restart it, restart the menu bar app, refresh statusline scripts that already exist, doctor.
# Scripts are only regenerated where a previous `ccs --<flag>` / generate created them; nothing
# new is written into Claude config dirs.
#
# Overridable for tests: CCS, APP, OPEN, OSASCRIPT, PY.
set -euo pipefail

CCS="${CCS:-$HOME/.local/bin/ccs}"
APP="${APP:-$HOME/Applications/CC Supervisor.app}"
OPEN="${OPEN:-open}"
OSASCRIPT="${OSASCRIPT:-osascript}"
PY="${PY:-python3}"
PLIST="$HOME/Library/LaunchAgents/local.ccsupervisor.daemon.plist"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ccs"

step() { printf '\n==> %s\n' "$*"; }

if [ ! -x "$CCS" ]; then
    echo "ccs not found at $CCS (run: make install)" >&2
    exit 1
fi

step "Daemon"
if [ -f "$PLIST" ]; then
    "$CCS" daemon install # rewrites the plist, then bootout + bootstrap (idempotent)
else
    echo "  not installed (run: ccs daemon install)"
fi

step "Menu bar app"
if [ -d "$APP" ]; then
    "$OSASCRIPT" -e 'quit app "CC Supervisor"' >/dev/null 2>&1 || true
    sleep 1
    "$OPEN" "$APP"
    echo "  restarted"
else
    echo "  not installed (run: make app)"
fi

step "Statusline scripts"
refreshed=0
if [ -f "$CONFIG_DIR/config.json" ]; then
    while IFS=$'\t' read -r pid pdir; do
        [ -n "$pid" ] || continue
        if [ -f "$pdir/ccs-statusline.py" ]; then
            if "$CCS" statusline generate --profile "$pid"; then
                refreshed=$((refreshed + 1))
            else
                echo "  ! $pid: not regenerated"
            fi
        fi
    done < <("$CCS" profile list --json | "$PY" -c '
import json, os, sys
for p in json.load(sys.stdin).get("profiles", []):
    print(p["id"] + "\t" + os.path.expanduser(p["config_dir"]).rstrip("/"))
')
fi
[ "$refreshed" = 0 ] && echo "  none to refresh"

step "ccs doctor"
"$CCS" doctor || true
