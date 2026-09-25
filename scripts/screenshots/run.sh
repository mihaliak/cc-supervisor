#!/usr/bin/env bash
# `make screenshots`: README images from the real UI, never from personal data.
#   1. demo_env.py builds an anonymized HOME, config and state (fake `claude`, example accounts)
#   2. capture_cli.sh records `ccs` output (ANSI) in that environment
#   3. the harness (the app + widget sources built with -D SCREENSHOTS) renders the real
#      SwiftUI views and the captures into PNGs
# ONLY=widgets,menu limits it to some images; OUT and WORK move the output and work dirs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/docs/assets/screenshots}"
WORK="${WORK:-$ROOT/build/screenshots}"
PY="${PY:-$ROOT/python/.venv/bin/python}"
DEMO="$WORK/demo"
ANSI_DIR="$WORK/ansi"

mkdir -p "$WORK" "$OUT"
# The harness builds first: the demo state is time-relative, so it's created right before rendering.
echo "==> harness"
app_sources=()
while IFS= read -r f; do app_sources+=("$f"); done < <(cd "$ROOT/macos" && find App Shared -name '*.swift' ! -name CCSupervisorApp.swift | sort)
(
    cd "$ROOT/macos"
    swiftc -parse-as-library -swift-version 6 -D SCREENSHOTS -O \
        -target arm64-apple-macos26.0 -module-name CCSupervisorScreenshots \
        "${app_sources[@]}" \
        Widgets/Views/Components.swift Widgets/Views/FamilyViews.swift Widgets/Timeline/UsageEntry.swift \
        Screenshots/*.swift \
        -o "$WORK/harness"
)

echo "==> demo environment + ccs captures"
"$PY" "$ROOT/scripts/screenshots/demo_env.py" --root "$DEMO" >"$WORK/env.sh"
bash "$ROOT/scripts/screenshots/capture_cli.sh" "$DEMO" "$ANSI_DIR"
# The app runs `~/.local/bin/ccs`; in the harness `~` is the demo HOME.
mkdir -p "$DEMO/home/.local/bin"
ln -sf "$ROOT/python/.venv/bin/ccs" "$DEMO/home/.local/bin/ccs"

echo "==> render"
(
    set -a
    # shellcheck disable=SC1091
    . "$WORK/env.sh"
    set +a
    "$WORK/harness" --out "$OUT" --ansi "$ANSI_DIR" --icon "$ROOT/macos/Branding/AppIcon-1024.png" ${ONLY:+--only "$ONLY"}
)
