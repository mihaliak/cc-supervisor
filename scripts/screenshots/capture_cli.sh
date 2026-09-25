#!/usr/bin/env bash
# Terminal captures for the README screenshots, from the anonymized demo environment.
#
#   scripts/screenshots/capture_cli.sh <demo-dir> <out-dir> [--exact]
#
# Rebuilds <demo-dir> (demo_env.py wipes it; `--exact` is passed on), runs a demo daemon on it,
# and writes <out-dir>/<name>.ansi (output, ANSI kept) plus <out-dir>/<name>.cmd (the command
# as typed). Demo paths become `~` forms; any leftover personal data fails the run.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <demo-dir> <out-dir> [--exact]" >&2
    exit 2
fi
started=$SECONDS
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
py="$repo/python/.venv/bin/python"
demo_env="$here/demo_env.py"
demo=$1
out=$2
shift 2
[[ -x "$py" ]] || { echo "missing $py (run: make venv)" >&2; exit 1; }

abspath() { case $1 in /*) printf '%s\n' "${1%/}" ;; *) printf '%s\n' "$PWD/${1%/}" ;; esac }
case "$(abspath "$out")/" in
    "$(abspath "$demo")/"*)
        echo "out-dir must be outside the demo dir (it is wiped on every run)" >&2
        exit 2
        ;;
esac
mkdir -p "$out"
out=$(cd "$out" && pwd)

# the demo env: fake HOME, XDG config/state, fake claude and launchctl first on PATH
exports=$("$py" "$demo_env" --root "$demo" "$@")
eval "$exports"
unset CLAUDE_CONFIG_DIR CCS_PROFILE CCS_WRAPPER_ID CLAUDECODE
export COLUMNS=100 LINES=50 TERM=xterm-256color

ccs config validate >/dev/null

"$py" "$demo_env" --root "$demo" --serve >"$demo/run/serve.out" 2>&1 &
daemon_pid=$!
stop_daemon() {
    if kill -0 "$daemon_pid" 2>/dev/null; then
        kill -TERM "$daemon_pid"
        wait "$daemon_pid" 2>/dev/null || true
    fi
}
trap stop_daemon EXIT
for _ in $(seq 1 100); do # ready = socket up and the first polls done
    [[ -f "$demo/run/daemon.pid" ]] && break
    kill -0 "$daemon_pid" 2>/dev/null || break
    sleep 0.1
done
if [[ ! -f "$demo/run/daemon.pid" ]]; then
    echo "demo daemon did not start:" >&2
    cat "$demo/run/serve.out" >&2
    exit 1
fi

rm -f "$out"/*.ansi "$out"/*.cmd
capture() { # capture <name> <command line, as the user types it>
    local name=$1 cmd=$2 rc=0
    printf '%s\n' "$cmd" >"$out/$name.cmd"
    bash -c "$cmd" >"$out/$name.ansi" 2>&1 || rc=$?
    printf '  %-22s rc=%s  %s\n' "$name" "$rc" "$cmd"
}

echo "capturing into $out"
capture status 'ccs status'
capture usage 'ccs usage'
capture sessions 'ccs sessions'
capture events 'ccs events'
capture statusline_work 'ccs statusline preview --profile work'
capture statusline_personal 'ccs statusline preview --profile personal'
capture profile_list 'ccs profile list'
# shellcheck disable=SC2016 # $p expands in the capture's shell
capture auth_status 'for p in personal work client; do ccs auth status --profile $p; done'
for p in personal work client; do
    capture "auth_status_$p" "ccs auth status --profile $p"
done
capture daemon_status 'ccs daemon status'
capture doctor 'ccs doctor'
# `ccs --work` while paused: the launcher's own question (no newline: it waits for y/N)
printf '%s\n' 'ccs --work' >"$out/launcher_prompt.cmd"
"$py" "$demo_env" --root "$demo" --launcher-prompt work >"$out/launcher_prompt.ansi"
printf '  %-22s rc=0  %s\n' launcher_prompt 'ccs --work'

stop_daemon
"$py" "$demo_env" --root "$demo" --sanitize "$out"
echo "done in $((SECONDS - started))s; no personal data found"
