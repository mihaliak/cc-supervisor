#!/usr/bin/env bash
# Keep exactly one registered copy of the app in LaunchServices: the installed one.
# Build folders and deleted worktrees leave extra registrations of the same bundle id
# behind, and macOS may then serve stale widget/App Intents metadata from them.
set -euo pipefail

KEEP="${1:-$HOME/Applications/CC Supervisor.app}"
LSREGISTER="${LSREGISTER:-/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister}"

[ -x "$LSREGISTER" ] || { echo "lsregister not found; skipping"; exit 0; }

removed=0
while IFS= read -r path; do
    [ -n "$path" ] || continue
    [ "$path" = "$KEEP" ] && continue
    "$LSREGISTER" -u "$path" >/dev/null 2>&1 || true
    removed=$((removed + 1))
done < <("$LSREGISTER" -dump 2>/dev/null \
    | sed -n 's/^path: *\(.*\/CC Supervisor\.app\) (0x[0-9a-f]*)$/\1/p' | sort -u)

if [ -d "$KEEP" ]; then
    "$LSREGISTER" -f -R "$KEEP" >/dev/null 2>&1 || true
fi
echo "launchservices: unregistered $removed stale copies; registered $KEEP"
