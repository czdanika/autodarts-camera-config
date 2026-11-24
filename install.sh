#!/usr/bin/env bash
# Autodarts Camera Config – installer
# Usage (on the Raspberry Pi / Linux box that runs Autodarts):
#   curl -fsSL https://raw.githubusercontent.com/czdanika/autodarts-camera-config/main/install.sh | bash
# Options (environment variables):
#   CAMCTL_PORT=8088            web port
#   CAMCTL_DIR=/opt/autodarts-camera-config   install directory
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/czdanika/autodarts-camera-config/main"
PORT="${CAMCTL_PORT:-8088}"
DIR="${CAMCTL_DIR:-/opt/autodarts-camera-config}"
SERVICE="autodarts-camera-config"
RUN_USER="${SUDO_USER:-$(id -un)}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }

[ "$(uname -s)" = "Linux" ] || { echo "This installer must run on the Linux machine that runs Autodarts."; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required."; exit 1; }
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"

say "Installing dependencies (python3, v4l-utils)…"
if ! command -v v4l2-ctl >/dev/null || ! command -v python3 >/dev/null; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq python3 v4l-utils curl
fi

say "Installing to $DIR …"
$SUDO mkdir -p "$DIR/fonts"
SRC_DIR=""
if [ -f "${BASH_SOURCE[0]:-}" ]; then SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/camctl.py" ]; then
  $SUDO cp "$SRC_DIR/camctl.py" "$DIR/camctl.py"
else
  $SUDO curl -fsSL "$REPO_RAW/camctl.py" -o "$DIR/camctl.py"
fi
$SUDO chown -R "$RUN_USER" "$DIR"

# Optional: borrow the Open Sans web fonts from the local Autodarts UI so the
# page looks identical even without internet access. Falls back to system fonts.
CSS_PATH="$(curl -fsS --max-time 3 http://localhost:3180/ 2>/dev/null | grep -o '/assets/index-[A-Za-z0-9_-]*\.css' | head -1 || true)"
if [ -n "$CSS_PATH" ]; then
  CSS="$(curl -fsS --max-time 5 "http://localhost:3180$CSS_PATH" || true)"
  for w in 400 600; do for s in latin latin-ext; do
    f="$(printf '%s' "$CSS" | grep -o "open-sans-$s-$w-normal-[A-Za-z0-9_-]*\.woff2" | head -1 || true)"
    [ -n "$f" ] && curl -fsS --max-time 5 "http://localhost:3180/assets/$f" -o "$DIR/fonts/open-sans-$s-$w.woff2" || true
  done; done
  say "Fonts copied from the local Autodarts UI."
else
  warn "Autodarts UI not reachable on :3180 – using system fonts (purely cosmetic)."
fi

say "Adding $RUN_USER to the 'video' group…"
$SUDO usermod -aG video "$RUN_USER" || true

say "Creating systemd service '$SERVICE' (port $PORT)…"
$SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Autodarts Camera Config (web UI for v4l2 camera settings)
After=network-online.target autodarts.service
Wants=network-online.target

[Service]
User=$RUN_USER
SupplementaryGroups=video
WorkingDirectory=$DIR
Environment=CAMCTL_PORT=$PORT
ExecStart=/usr/bin/python3 $DIR/camctl.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE" >/dev/null 2>&1
$SUDO systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  say "Done! Open:  http://${IP:-<pi-ip>}:$PORT"
  echo "    Cameras found: $(v4l2-ctl --list-devices 2>/dev/null | grep -c 'usb-' || true)"
else
  warn "The service did not start. Check: journalctl -u $SERVICE -n 50"
  exit 1
fi
