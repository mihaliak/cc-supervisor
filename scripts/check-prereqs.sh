#!/bin/sh
# Prerequisites for `make install` (P13). One line per check; exits 1 when a hard requirement
# is missing. `claude` is only a warning: ccs installs without it, but can't supervise anything.
# PY: the interpreter `make` resolved (default: python3 on PATH).
set -eu

PY="${PY:-python3}"
status=0

ok() { printf '  ✓ %s\n' "$1"; }
bad() {
    printf '  ✗ %s\n      fix: %s\n' "$1" "$2"
    status=1
}
warn() { printf '  ! %s\n      hint: %s\n' "$1" "$2"; }

major_of() {
    # "26.6.2" -> 26; anything non-numeric -> 0
    m="${1%%.*}"
    case "$m" in
        '' | *[!0-9]*) echo 0 ;;
        *) echo "$m" ;;
    esac
}

echo "Checking prerequisites"

macos="$(sw_vers -productVersion 2>/dev/null || true)"
if [ "$(major_of "$macos")" -ge 26 ]; then
    ok "macOS $macos"
else
    bad "macOS ${macos:-unknown}: need macOS 26 or newer" "update macOS (System Settings › General › Software Update)"
fi

xcode="$(xcodebuild -version 2>/dev/null | sed -n 's/^Xcode //p' | head -n 1 || true)"
if [ "$(major_of "$xcode")" -ge 26 ]; then
    ok "Xcode $xcode"
else
    bad "Xcode ${xcode:-not found}: need Xcode 26 or newer" "install Xcode 26 from the App Store, then: sudo xcode-select -s /Applications/Xcode.app"
fi

if command -v xcodegen >/dev/null 2>&1; then
    ok "xcodegen $(xcodegen --version 2>/dev/null | sed -n 's/^Version: //p' | head -n 1)"
else
    bad "xcodegen missing" "brew install xcodegen"
fi

if pyver="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]); sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null)"; then
    ok "Python $pyver ($PY)"
else
    bad "Python ${pyver:-not found} ($PY): need Python 3.12 or newer" "brew install python@3.14"
fi

if command -v pipx >/dev/null 2>&1; then
    ok "pipx $(pipx --version 2>/dev/null | head -n 1)"
else
    bad "pipx missing" "brew install pipx && pipx ensurepath"
fi

if command -v claude >/dev/null 2>&1; then
    ok "claude $(claude --version 2>/dev/null | head -n 1)"
else
    warn "claude not on PATH (ccs installs, but can't supervise anything yet)" "install Claude Code: https://claude.com/claude-code"
fi

if [ "$status" -ne 0 ]; then
    echo "Missing prerequisites: fix the items marked ✗ and run make install again."
fi
exit "$status"
