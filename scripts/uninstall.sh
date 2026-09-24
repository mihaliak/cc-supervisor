#!/usr/bin/env bash
# `make uninstall` (P13): revert applied statuslines, remove the daemon, the login item, the app
# and the ccs CLI. Config and state are deleted only on an explicit yes (or PURGE=1).
# Claude config dirs and their logins are never touched, except that the statusLine ccs applied
# is restored and, on purge, the generated ccs-statusline.py files are removed.
#
# YES=1 skips the confirmations (scripted runs); the purge still needs PURGE=1.
# Overridable for tests: CCS, APP, PIPX, OSASCRIPT, PY.
set -euo pipefail

CCS="${CCS:-$HOME/.local/bin/ccs}"
APP="${APP:-$HOME/Applications/CC Supervisor.app}"
PIPX="${PIPX:-pipx}"
OSASCRIPT="${OSASCRIPT:-osascript}"
PY="${PY:-python3}"
YES="${YES:-}"
PURGE="${PURGE:-}"
LABEL="local.ccsupervisor.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ccs"
if [ -n "${CCS_STATE_DIR:-}" ]; then
    STATE_DIR="$CCS_STATE_DIR"
else
    STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ccs"
fi

step() { printf '\n==> %s\n' "$*"; }

ask() {
    # ask "question" -> 0 on y/yes; default no
    local answer=""
    printf '%s [y/N] ' "$1"
    read -r answer || answer=""
    case "$answer" in
        y | Y | yes | YES) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -z "$YES" ]; then
    if ! ask "Uninstall CC Supervisor (daemon, menu bar app, login item, ccs CLI)?"; then
        echo "aborted"
        exit 1
    fi
fi

have_ccs=0
[ -x "$CCS" ] && have_ccs=1

# profile id + expanded config dir, collected while ccs still exists
profile_ids=()
profile_dirs=()
if [ "$have_ccs" = 1 ] && [ -f "$CONFIG_DIR/config.json" ]; then
    while IFS=$'\t' read -r pid pdir; do
        [ -n "$pid" ] || continue
        profile_ids+=("$pid")
        profile_dirs+=("$pdir")
    done < <("$CCS" profile list --json | "$PY" -c '
import json, os, sys
for p in json.load(sys.stdin).get("profiles", []):
    print(p["id"] + "\t" + os.path.expanduser(p["config_dir"]).rstrip("/"))
')
fi

step "Statuslines"
reverted=0
for pid in "${profile_ids[@]+"${profile_ids[@]}"}"; do
    if [ -f "$STATE_DIR/statusline/$pid.json" ]; then
        if "$CCS" statusline revert --profile "$pid"; then
            reverted=$((reverted + 1))
        else
            echo "  ! $pid: not reverted (see above); its settings.json was left as is"
        fi
    fi
done
[ "$reverted" = 0 ] && echo "  nothing applied by ccs"

step "Daemon"
if [ "$have_ccs" = 1 ]; then
    "$CCS" daemon uninstall || echo "  ! ccs daemon uninstall failed"
elif [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    rm -f "$PLIST"
fi
echo "  LaunchAgent removed"

step "Menu bar app"
if [ -d "$APP" ]; then
    "$APP/Contents/MacOS/CC Supervisor" --unregister-login-item ||
        echo "  ! login item not unregistered: remove it in System Settings › General › Login Items"
    "$OSASCRIPT" -e 'quit app "CC Supervisor"' >/dev/null 2>&1 || true
    rm -rf "$APP"
    echo "  removed $APP"
else
    echo "  not installed"
fi

step "ccs CLI"
if "$PIPX" uninstall cc-supervisor >/dev/null 2>&1; then
    echo "  pipx package cc-supervisor removed"
else
    echo "  pipx package cc-supervisor was not installed"
fi

step "Config and state"
scripts=()
for dir in "${profile_dirs[@]+"${profile_dirs[@]}"}"; do
    [ -f "$dir/ccs-statusline.py" ] && scripts+=("$dir/ccs-statusline.py")
done
echo "  config:     $CONFIG_DIR"
echo "  state:      $STATE_DIR"
for script in "${scripts[@]+"${scripts[@]}"}"; do
    echo "  statusline: $script"
done
purge=0
if [ "$PURGE" = 1 ]; then
    purge=1
elif [ -z "$YES" ] && ask "Also delete config (~/.config/ccs) and state (~/.local/state/ccs)?"; then
    purge=1
fi
if [ "$purge" = 1 ]; then
    rm -rf "$CONFIG_DIR" "$STATE_DIR"
    for script in "${scripts[@]+"${scripts[@]}"}"; do
        rm -f "$script"
    done
    echo "  deleted"
else
    echo "  kept (delete later with: make uninstall PURGE=1, or remove the paths above)"
fi

echo
echo "CC Supervisor is uninstalled. Claude config dirs and logins were not touched."
