#!/usr/bin/env bash
# `make upgrade` (P13), in two calls:
#
#   upgrade.sh --stop-daemon   before `install-dev`: pipx --force deletes and recreates the venv
#                              the daemon runs from, and KeepAlive would respawn a daemon that
#                              crashed mid-swap every 10 s. So the job is unloaded (bootout)
#                              first. Uses launchctl directly: no working ccs is needed.
#   upgrade.sh                 after `install-dev` and `app`: reinstall the daemon's LaunchAgent
#                              (new plist, e.g. `run --launchd` and the `CCS_STATE_DIR`
#                              passthrough) which also starts it again, restart the menu bar app,
#                              refresh statusline scripts that already exist, doctor.
#
# Scripts are only regenerated where a previous `ccs --<flag>` / generate created them; nothing
# new is written into Claude config dirs. Exits 1 when a statusline script couldn't be refreshed.
#
# Overridable for tests: CCS, APP, OPEN, OSASCRIPT, PY, WAIT_S.
set -euo pipefail

CCS="${CCS:-$HOME/.local/bin/ccs}"
APP="${APP:-$HOME/Applications/CC Supervisor.app}"
OPEN="${OPEN:-open}"
OSASCRIPT="${OSASCRIPT:-osascript}"
PY="${PY:-python3}"
WAIT_S="${WAIT_S:-10}"
LABEL="local.ccsupervisor.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# the dir ccs uses (ccs.paths: XDG_CONFIG_HOME only when absolute)
case "${XDG_CONFIG_HOME:-}" in
    /*) CONFIG_DIR="$XDG_CONFIG_HOME/ccs" ;;
    *) CONFIG_DIR="$HOME/.config/ccs" ;;
esac

step() { printf '\n==> %s\n' "$*"; }

if [ "${1:-}" = "--stop-daemon" ]; then
    step "Stopping the daemon (while ccs is reinstalled)"
    target="gui/$(id -u)/$LABEL"
    if [ -f "$PLIST" ] || launchctl print "$target" >/dev/null 2>&1; then
        launchctl bootout "$target" >/dev/null 2>&1 || true
        # bootout returns before the job is gone; wait so KeepAlive can't restart it mid-swap
        for _ in $(seq 1 "$WAIT_S"); do
            launchctl print "$target" >/dev/null 2>&1 || break
            sleep 1
        done
        echo "  stopped (the upgrade starts it again)"
    else
        echo "  not installed"
    fi
    exit 0
fi

if [ ! -x "$CCS" ]; then
    echo "ccs not found at $CCS (run: make install)" >&2
    exit 1
fi

status=0

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
    # captured first: a failing `ccs profile list` must not look like "no profiles"
    if listing="$("$CCS" profile list --json)" && profiles="$(printf '%s' "$listing" | "$PY" -c '
import json, os, sys
for p in json.load(sys.stdin)["profiles"]:
    print(p["id"] + "\t" + os.path.expanduser(p["config_dir"]).rstrip("/"))
')"; then
        while IFS=$'\t' read -r pid pdir; do
            [ -n "$pid" ] || continue
            if [ -f "$pdir/ccs-statusline.py" ]; then
                if "$CCS" statusline generate --profile "$pid"; then
                    refreshed=$((refreshed + 1))
                else
                    echo "  ! $pid: not regenerated"
                    status=1
                fi
            fi
        done <<<"$profiles"
        [ "$refreshed" != 0 ] || [ "$status" != 0 ] || echo "  none to refresh"
    else
        echo "  ! cannot list the profiles (ccs profile list --json failed):" \
            "existing statusline scripts were not refreshed" >&2
        status=1
    fi
else
    echo "  none to refresh"
fi

step "ccs doctor"
"$CCS" doctor || true

if [ "$status" != 0 ]; then
    echo >&2
    echo "upgrade incomplete: fix the ! items above, then run make upgrade again" >&2
fi
exit "$status"
