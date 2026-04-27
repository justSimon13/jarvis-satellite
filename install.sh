#!/bin/bash
# J.A.R.V.I.S. Satellite Installer — Linux (Headless Audio Client)
# Voraussetzung: git, python3.11+, pip
# Ausführen als normaler User mit sudo-Rechten.

set -e

REPO="https://github.com/justSimon13/jarvis-satellite.git"
INSTALL_DIR="$HOME/jarvis-satellite"
SERVICE_NAME="jarvis-client"
PYTHON=$(command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null)

echo ""
echo "════════════════════════════════════════"
echo "  J.A.R.V.I.S. Satellite Installer"
echo "════════════════════════════════════════"
echo ""

# ── 1. System-Pakete ──────────────────────────────────────────────────────────
echo "Installiere System-Abhängigkeiten..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git python3 python3-pip python3-venv curl \
    portaudio19-dev ffmpeg libsndfile1
echo "✓ System-Pakete"

# ── 2. Repo klonen oder aktualisieren ─────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Aktualisiere bestehendes Repo..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "Klone Repo nach $INSTALL_DIR ..."
    git clone "$REPO" "$INSTALL_DIR"
fi
echo "✓ Code"

# ── 3. Virtuelle Umgebung ─────────────────────────────────────────────────────
echo "Erstelle Python-Umgebung..."
"$PYTHON" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "✓ Python-Umgebung"

# ── 4. .env anlegen (wenn nicht vorhanden) ────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo "✓ .env erstellt — Server-IP bitte eintragen: nano $INSTALL_DIR/.env"
else
    echo "✓ .env bereits vorhanden"
fi

# ── 5. systemd User-Service ───────────────────────────────────────────────────
mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/${SERVICE_NAME}.service" << EOF
[Unit]
Description=J.A.R.V.I.S. Audio Client
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python3 $INSTALL_DIR/client.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

# ── 6. Auto-Update Timer (alle 30 Min, prüft GitHub Releases) ─────────────────
chmod +x "$INSTALL_DIR/update.sh"

cat > "$HOME/.config/systemd/user/jarvis-update.service" << EOF
[Unit]
Description=J.A.R.V.I.S. Satellite Update Check

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/update.sh
StandardOutput=journal
StandardError=journal
EOF

cat > "$HOME/.config/systemd/user/jarvis-update.timer" << EOF
[Unit]
Description=J.A.R.V.I.S. Satellite Update Check alle 30 Min

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user enable jarvis-update.timer
systemctl --user start jarvis-update.timer
echo "✓ systemd Service + Auto-Update Timer registriert"

# ── Fertig ────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  Installation abgeschlossen!"
echo "════════════════════════════════════════"
echo ""
echo "  Nächste Schritte:"
echo "  1. Server-IP eintragen:   nano $INSTALL_DIR/.env"
echo "  2. Client starten:        systemctl --user start $SERVICE_NAME"
echo "  3. Logs verfolgen:        journalctl --user -u $SERVICE_NAME -f"
echo ""
echo "  Auto-Update: läuft alle 30 Min, prüft auf neues GitHub-Release."
echo "  Manuell updaten: bash $INSTALL_DIR/update.sh"
echo ""
