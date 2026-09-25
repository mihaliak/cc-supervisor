#!/usr/bin/env bash
# `make install`, after `install-dev` (ccs via pipx) and `app` (~/Applications) ran (P13).
#
# Seeds the config if missing, installs + starts the daemon LaunchAgent, registers the login
# item, opens the menu bar app, runs `ccs doctor` and prints the next steps.
# Never touches a Claude config dir (~/.claude*): no statusline generate/apply, no settings.
#
# Overridable for tests: CCS, APP, OPEN, WAIT_S.
set -euo pipefail

CCS="${CCS:-$HOME/.local/bin/ccs}"
APP="${APP:-$HOME/Applications/CC Supervisor.app}"
OPEN="${OPEN:-open}"
WAIT_S="${WAIT_S:-20}"

# the dir ccs uses (ccs.paths: an env override counts only when it is an absolute path)
case "${CCS_STATE_DIR:-}" in
    /*) STATE_DIR="$CCS_STATE_DIR" ;;
    *)
        case "${XDG_STATE_HOME:-}" in
            /*) STATE_DIR="$XDG_STATE_HOME/ccs" ;;
            *) STATE_DIR="$HOME/.local/state/ccs" ;;
        esac
        ;;
esac

step() { printf '\n==> %s\n' "$*"; }

if [ ! -x "$CCS" ]; then
    echo "ccs not found at $CCS (run: make install-dev)" >&2
    exit 1
fi

step "Config"
"$CCS" profile list # creates the config with the default profiles on first run
"$CCS" config validate

step "Daemon (LaunchAgent)"
"$CCS" daemon install

step "Menu bar app"
if [ -d "$APP" ]; then
    "$APP/Contents/MacOS/CC Supervisor" --register-login-item ||
        echo "  ! login item not registered: turn on Settings › General › Launch at login"
    "$OPEN" "$APP"
else
    echo "  ! $APP not found (run: make app)"
fi

step "Waiting for the first usage poll (up to ${WAIT_S}s)"
snapshot="$STATE_DIR/widget/snapshot.json"
for _ in $(seq 1 "$WAIT_S"); do
    [ -f "$snapshot" ] && break
    sleep 1
done
if [ -f "$snapshot" ]; then echo "  ✓ usage data is in"; else echo "  ! no usage data yet"; fi

step "ccs doctor"
"$CCS" doctor || true

step "Next steps"
cat <<'EOF'
  1. Review your profiles (warm-ups are on by default):
       menu bar › Settings… › Profiles   or   ccs profile list
  2. Sign in each profile that shows "not signed in" above:
       ccs auth login --profile <id>     (or Settings › Profiles › Sign in)
  3. Start Claude Code under supervision:
       ccs --<flag>                      e.g. ccs --work
  4. Optional: show the statusline in plain `claude` too (edits that profile's settings.json,
     with a backup):
       ccs statusline apply --profile <id>
  5. Add widgets: right-click the desktop › Edit Widgets › search "CC Supervisor".
EOF
