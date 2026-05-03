#!/bin/bash
# J.A.R.V.I.S. Satellite Installer — Linux (Headless Audio Client)
# Voraussetzung: git, python3.11+, pip
# Ausführen als normaler User mit sudo-Rechten.

set -e

REPO="https://github.com/justSimon13/jarvis-satellite.git"
INSTALL_DIR="$HOME/jarvis-satellite"
SERVICE_NAME="jarvis-client"
PYTHON=$(command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || command -v python3 2>/dev/null)
USER_ID=$(id -u)
USER_NAME=$(whoami)

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
    portaudio19-dev ffmpeg libsndfile1 alsa-utils
echo "✓ System-Pakete"

# ── 2. User zur audio-Gruppe hinzufügen ───────────────────────────────────────
if ! groups "$USER_NAME" | grep -q '\baudio\b'; then
    echo "Füge $USER_NAME zur audio-Gruppe hinzu..."
    sudo usermod -aG audio "$USER_NAME"
    echo "✓ audio-Gruppe — wirkt ab nächstem Login"
else
    echo "✓ audio-Gruppe bereits gesetzt"
fi

# ── 3. Repo klonen oder aktualisieren ─────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Aktualisiere bestehendes Repo..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "Klone Repo nach $INSTALL_DIR ..."
    git clone "$REPO" "$INSTALL_DIR"
fi
echo "✓ Code"

# ── 4. Virtuelle Umgebung ─────────────────────────────────────────────────────
echo "Erstelle Python-Umgebung..."
"$PYTHON" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "✓ Python-Umgebung"

# ── 5. .env konfigurieren ─────────────────────────────────────────────────────
# Werte können als Env-Variablen übergeben werden, z.B.:
#   JARVIS_SERVER=ws://localhost:8765 CLIENT_NAME=wohnzimmer bash install.sh
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

_env_set() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$INSTALL_DIR/.env" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$INSTALL_DIR/.env"
    else
        echo "${key}=${val}" >> "$INSTALL_DIR/.env"
    fi
}

[ -n "$JARVIS_SERVER"       ] && _env_set "JARVIS_SERVER"       "$JARVIS_SERVER"
[ -n "$CLIENT_NAME"         ] && _env_set "CLIENT_NAME"         "$CLIENT_NAME"
[ -n "$MANUAL_MODE"         ] && _env_set "MANUAL_MODE"         "$MANUAL_MODE"
[ -n "$AUDIO_INPUT_DEVICE"  ] && _env_set "AUDIO_INPUT_DEVICE"  "$AUDIO_INPUT_DEVICE"
[ -n "$AUDIO_OUTPUT_DEVICE" ] && _env_set "AUDIO_OUTPUT_DEVICE" "$AUDIO_OUTPUT_DEVICE"
[ -n "$BT_SPEAKER_MAC"      ] && _env_set "BT_SPEAKER_MAC"      "$BT_SPEAKER_MAC"

echo "✓ .env konfiguriert"
if ! grep -q "^JARVIS_SERVER=ws://" "$INSTALL_DIR/.env" 2>/dev/null; then
    echo "  ⚠ JARVIS_SERVER nicht gesetzt — bitte nachtragen: nano $INSTALL_DIR/.env"
fi

# ── 6. ALSA-Default konfigurieren (Mic + Speaker getrennt) ───────────────────
# Erkennt automatisch: erstes USB-Capture-Gerät als Mic, erstes USB-Playback-Gerät als Speaker
MIC_CARD=$(aplay -l 2>/dev/null | grep -i "usb\|uac\|jieli\|pebble" | head -1 | grep -o 'card [0-9]*' | grep -o '[0-9]*' || echo "")
SPK_CARD=$(aplay -l 2>/dev/null | grep -i "usb\|pebble\|speaker" | head -1 | grep -o 'card [0-9]*' | grep -o '[0-9]*' || echo "")

if [ -n "$MIC_CARD" ] || [ -n "$SPK_CARD" ]; then
    MIC_CARD=${MIC_CARD:-0}
    SPK_CARD=${SPK_CARD:-0}
    cat > "$HOME/.asoundrc" << ASOUND
pcm.!default {
    type asym
    playback.pcm {
        type plug
        slave.pcm "hw:${SPK_CARD},0"
    }
    capture.pcm {
        type plug
        slave.pcm "hw:${MIC_CARD},0"
    }
}
ctl.!default {
    type hw
    card ${MIC_CARD}
}
ASOUND
    echo "✓ ALSA konfiguriert — Mic: card $MIC_CARD, Speaker: card $SPK_CARD"
else
    echo "⚠ ALSA-Geräte nicht erkannt — .asoundrc nicht gesetzt"
fi

# ── 7. Alten User-Service aufräumen (falls vorhanden) ────────────────────────
if systemctl --user is-enabled "$SERVICE_NAME" 2>/dev/null | grep -q "enabled"; then
    echo "Deaktiviere alten User-Service..."
    systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
fi
# Manuell gestartete PulseAudio-Instanz stoppen (System-Service braucht kein PA)
pulseaudio --kill 2>/dev/null || true

# ── 9. systemd System-Service (kein User-Service) ─────────────────────────────
# System-Service mit expliziter Gruppe: funktioniert unabhängig von Login-Session
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=J.A.R.V.I.S. Audio Client
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
Group=audio
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python3 ${INSTALL_DIR}/client.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=HOME=${HOME}
Environment=XDG_RUNTIME_DIR=/run/user/${USER_ID}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "✓ systemd System-Service registriert"

# ── 10. Auto-Update Timer ─────────────────────────────────────────────────────
chmod +x "$INSTALL_DIR/update.sh"

sudo tee /etc/systemd/system/jarvis-update.service > /dev/null << EOF
[Unit]
Description=J.A.R.V.I.S. Satellite Update Check

[Service]
Type=oneshot
User=${USER_NAME}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/update.sh
StandardOutput=journal
StandardError=journal
EOF

sudo tee /etc/systemd/system/jarvis-update.timer > /dev/null << EOF
[Unit]
Description=J.A.R.V.I.S. Satellite Update Check alle 30 Min

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable jarvis-update.timer
sudo systemctl start jarvis-update.timer
echo "✓ Auto-Update Timer registriert"

# ── Fertig ────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  Installation abgeschlossen!"
echo "════════════════════════════════════════"
echo ""
echo "  Nächste Schritte:"
if ! grep -q "^JARVIS_SERVER=ws://" "$INSTALL_DIR/.env" 2>/dev/null; then
echo "  1. Server-IP eintragen:   nano $INSTALL_DIR/.env"
echo "  2. Client starten:        sudo systemctl start $SERVICE_NAME"
else
echo "  1. Client starten:        sudo systemctl start $SERVICE_NAME"
fi
echo "  Logs verfolgen:           journalctl -u $SERVICE_NAME -f"
echo ""
echo "  Auto-Update: läuft alle 30 Min, prüft auf neues GitHub-Release."
echo "  Manuell updaten: bash $INSTALL_DIR/update.sh"
echo ""
