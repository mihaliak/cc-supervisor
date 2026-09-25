#!/usr/bin/env bash
# `make uninstall` (P13): revert applied statuslines, remove the daemon, the login item, the app
# and the ccs CLI. Config and state are deleted only on an explicit yes (or PURGE=1).
# Claude config dirs and their logins are never touched, except that the statusLine ccs applied
# is restored and, on purge, the generated ccs-statusline.py files are removed.
#
# Statuslines are reverted from the bookkeeping in $STATE_DIR/statusline/<profile>.json: by
# `ccs statusline revert`, or by an inline stdlib python3 restore when ccs can't run or refuses
# (profile removed, invalid config). A record whose statusLine wasn't restored is never deleted,
# not even on purge.
#
# YES=1 skips the confirmations (scripted runs); the purge still needs PURGE=1.
# Overridable for tests: CCS, APP, PIPX, OSASCRIPT, PY.
set -euo pipefail

APP="${APP:-$HOME/Applications/CC Supervisor.app}"
PIPX="${PIPX:-pipx}"
OSASCRIPT="${OSASCRIPT:-osascript}"
PY="${PY:-python3}"
YES="${YES:-}"
PURGE="${PURGE:-}"
LABEL="local.ccsupervisor.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# the dirs ccs uses (ccs.paths: an env override counts only when it is an absolute path)
case "${XDG_CONFIG_HOME:-}" in
    /*) CONFIG_DIR="$XDG_CONFIG_HOME/ccs" ;;
    *) CONFIG_DIR="$HOME/.config/ccs" ;;
esac
case "${CCS_STATE_DIR:-}" in
    /*) STATE_DIR="$CCS_STATE_DIR" ;;
    *)
        case "${XDG_STATE_HOME:-}" in
            /*) STATE_DIR="$XDG_STATE_HOME/ccs" ;;
            *) STATE_DIR="$HOME/.local/state/ccs" ;;
        esac
        ;;
esac
BOOK_DIR="$STATE_DIR/statusline"
NL=$'\n'

# `ccs statusline revert` from a bookkeeping record alone (argv[1]); any python3 >= 3.8.
# Prints why and exits 1 when the statusLine can't be restored; the record is then kept.
RESTORE_PY='
import json, os, shlex, sys, tempfile


def restore(book_path):
    try:
        with open(book_path, encoding="utf-8") as fh:
            book = json.load(fh)
        path = book["settings_path"]
        if not isinstance(path, str):
            return "the record has no settings path"
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "cannot read it: %s" % exc
    if not isinstance(settings, dict):
        return "%s is not a JSON object" % path
    value = settings.get("statusLine")
    command = value.get("command") if isinstance(value, dict) else None
    try:
        parts = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        parts = []
    script = os.path.join(os.path.dirname(path), "ccs-statusline.py")
    ours = isinstance(value, dict) and value.get("type") == "command"
    if not (ours and parts and parts[-1] == script and "-S" in parts and "-E" in parts):
        return "%s no longer points at the ccs statusline; edit statusLine by hand" % path
    previous = book.get("previous_statusline")
    if previous is None:
        settings.pop("statusLine", None)
    else:
        settings["statusLine"] = previous
    target = os.path.realpath(path)  # a symlinked settings.json (dotfiles) stays a symlink
    try:
        text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
        mode = os.stat(target).st_mode & 0o7777
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".settings.json.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError) as exc:
        return "cannot write %s: %s" % (path, exc)
    try:
        os.unlink(book_path)
    except OSError:
        pass
    return None


why = restore(sys.argv[1])
if why:
    print(why)
    sys.exit(1)
'

# The config dir a bookkeeping record was applied to (nothing when unreadable).
BOOK_DIR_PY='
import json, os, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(os.path.dirname(json.load(fh)["settings_path"]))
except Exception:
    pass
'

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

purge_refusal() {
    # purge_refusal <dir> <marker>... -> prints why <dir> must not be deleted recursively
    # (nothing when it may be): the filesystem root, $HOME or a parent of it, or a dir without
    # any of the markers ccs creates in it. A mis-set CCS_STATE_DIR / XDG_CONFIG_HOME (e.g.
    # $HOME) must never make a purge delete someone's files.
    local dir="$1" real home m
    shift
    if ! real="$(cd -P -- "$dir" 2>/dev/null && pwd)"; then
        echo "it can't be resolved"
        return 0
    fi
    home="$(cd -P -- "$HOME" 2>/dev/null && pwd)" || home="$HOME"
    if [ -z "$real" ] || [ "$real" = / ]; then
        echo "it is the filesystem root"
        return 0
    fi
    case "${home%/}/" in
        "$real/"*)
            echo "it is \$HOME or a folder containing it"
            return 0
            ;;
    esac
    for m in "$@"; do
        if [ -e "$real/$m" ]; then return 0; fi
    done
    echo "it doesn't look like a ccs folder (none of: $*)"
}

find_ccs() {
    # the first ccs that actually runs: $CCS, ~/.local/bin/ccs, then the one on PATH
    local c
    for c in "${CCS:-}" "$HOME/.local/bin/ccs" "$(command -v ccs 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c" ] || continue
        if "$c" --version >/dev/null 2>&1; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    return 1
}

if [ -z "$YES" ]; then
    if ! ask "Uninstall CC Supervisor (daemon, menu bar app, login item, ccs CLI)?"; then
        echo "aborted"
        exit 1
    fi
fi

CCS="$(find_ccs || true)"
have_ccs=0
[ -n "$CCS" ] && have_ccs=1

# the profiles' config dirs (for the generated scripts), read while the config still exists
script_dirs=""
listed=1
if [ -f "$CONFIG_DIR/config.json" ]; then
    if listing="$("$PY" -c '
import json, os, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    for p in json.load(fh).get("profiles", []):
        print(os.path.expanduser(p["config_dir"]).rstrip("/"))
' "$CONFIG_DIR/config.json" 2>/dev/null)"; then
        script_dirs="$listing$NL"
    else
        listed=0
    fi
fi

step "Statuslines"
reverted=0
kept=""
kept_n=0
if [ -d "$BOOK_DIR" ] && [ ! -r "$BOOK_DIR" ]; then
    echo "  ! cannot read $BOOK_DIR: statuslines applied by ccs were not reverted"
fi
for book in "$BOOK_DIR"/*.json; do
    [ -f "$book" ] || continue
    pid="$(basename "$book" .json)"
    dir="$("$PY" -c "$BOOK_DIR_PY" "$book" 2>/dev/null || true)"
    rc=1
    if [ "$have_ccs" = 1 ] && [ -f "$CONFIG_DIR/config.json" ]; then
        rc=0
        out="$("$CCS" statusline revert --profile "$pid" 2>&1)" || rc=$?
        [ "$rc" != 0 ] || echo "$out"
    fi
    if [ "$rc" != 0 ] && [ -f "$book" ]; then
        # ccs can't run, or refused (profile removed, invalid config): restore from the record
        rc=0
        why="$("$PY" -c "$RESTORE_PY" "$book" 2>&1)" || rc=$?
        if [ "$rc" = 0 ]; then
            echo "$pid: statusLine restored"
        else
            echo "  ! $pid: ${why:-python3 failed}"
        fi
    fi
    if [ "$rc" = 0 ]; then
        reverted=$((reverted + 1))
        [ -z "$dir" ] || script_dirs="$script_dirs$dir$NL"
    else
        kept="$kept$book$NL"
        kept_n=$((kept_n + 1))
        echo "  ! $pid: not reverted; its settings.json was left as is"
    fi
done
if [ "$kept_n" -gt 0 ]; then
    echo "  ! the previous statusLine of each profile above is kept in:"
    printf '%s' "$kept" | sed 's/^/      /'
elif [ "$reverted" = 0 ]; then
    echo "  nothing applied by ccs"
fi

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
scripts=""
while IFS= read -r dir; do
    [ -n "$dir" ] && [ -f "$dir/ccs-statusline.py" ] || continue
    case "$NL$scripts" in *"$NL$dir/ccs-statusline.py$NL"*) continue ;; esac
    scripts="$scripts$dir/ccs-statusline.py$NL"
done <<<"$script_dirs"
echo "  config:     $CONFIG_DIR"
echo "  state:      $STATE_DIR"
[ "$listed" = 1 ] ||
    echo "  ! cannot read the profiles in $CONFIG_DIR/config.json: their scripts aren't listed"
while IFS= read -r script; do
    [ -z "$script" ] || echo "  statusline: $script"
done <<<"$scripts"
purge=0
if [ "$PURGE" = 1 ]; then
    purge=1
elif [ -z "$YES" ] && ask "Also delete config (~/.config/ccs) and state (~/.local/state/ccs)?"; then
    purge=1
fi
refused=0
if [ "$purge" = 1 ]; then
    for dir in "$CONFIG_DIR" "$STATE_DIR"; do
        [ -e "$dir" ] || [ -L "$dir" ] || continue
        if [ "$dir" = "$CONFIG_DIR" ]; then
            why="$(purge_refusal "$dir" config.json)"
        else
            why="$(purge_refusal "$dir" statusline usage widget supervisor events.jsonl)"
        fi
        if [ -n "$why" ]; then
            echo "  ! not deleting $dir: $why. Check the environment (XDG_CONFIG_HOME," \
                "CCS_STATE_DIR, XDG_STATE_HOME) or delete it by hand."
            refused=1
        fi
    done
fi
if [ "$refused" = 1 ]; then
    echo "  nothing deleted"
elif [ "$purge" = 1 ]; then
    rm -rf "$CONFIG_DIR"
    if [ "$kept_n" = 0 ]; then
        rm -rf "$STATE_DIR"
    else
        # everything except the records of statusLines that weren't restored
        for entry in "$STATE_DIR"/* "$STATE_DIR"/.[!.]* "$BOOK_DIR"/* "$BOOK_DIR"/.[!.]*; do
            [ -e "$entry" ] || [ -L "$entry" ] || continue
            [ "$entry" != "$BOOK_DIR" ] || continue
            case "$NL$kept" in *"$NL$entry$NL"*) continue ;; esac
            rm -rf "$entry"
        done
    fi
    while IFS= read -r script; do
        [ -z "$script" ] || rm -f "$script"
    done <<<"$scripts"
    if [ "$kept_n" = 0 ]; then
        echo "  deleted"
    else
        echo "  deleted, except the statusline records listed above (in $BOOK_DIR)"
    fi
else
    echo "  kept (delete later with: make uninstall PURGE=1, or remove the paths above)"
fi

echo
echo "CC Supervisor is uninstalled. Claude config dirs and logins were not touched."
if [ "$refused" = 1 ]; then
    echo "Config and state were NOT deleted: see the ! lines above." >&2
    exit 1
fi
