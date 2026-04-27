#!/bin/bash
# Prüft ob ein neues GitHub-Release verfügbar ist und aktualisiert wenn ja.
# Wird von jarvis-update.timer alle 30 Minuten aufgerufen.

set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION_FILE="$INSTALL_DIR/.current_version"
SERVICE_NAME="jarvis-client"
API_URL="https://api.github.com/repos/justSimon13/jarvis-satellite/releases/latest"

current=$(cat "$VERSION_FILE" 2>/dev/null || echo "none")
latest=$(curl -fsSL "$API_URL" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "")

if [ -z "$latest" ]; then
    echo "[update] GitHub nicht erreichbar, übersprungen."
    exit 0
fi

if [ "$current" = "$latest" ]; then
    echo "[update] Bereits aktuell ($current)."
    exit 0
fi

echo "[update] Neues Release: $current → $latest"
git -C "$INSTALL_DIR" pull --ff-only
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "$latest" > "$VERSION_FILE"

systemctl --user restart "$SERVICE_NAME" && echo "[update] Service neugestartet." || true
echo "[update] Fertig."
