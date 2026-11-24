#!/usr/bin/env bash
# Autodarts Camera Config – uninstaller (camera settings themselves are not changed)
set -euo pipefail
SERVICE="autodarts-camera-config"
DIR="${CAMCTL_DIR:-/opt/autodarts-camera-config}"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
$SUDO systemctl disable --now "$SERVICE" 2>/dev/null || true
$SUDO rm -f "/etc/systemd/system/$SERVICE.service"
$SUDO systemctl daemon-reload
if [ "${1:-}" = "--purge" ]; then
  $SUDO rm -rf "$DIR"; echo "Removed service and $DIR (including saved settings)."
else
  echo "Service removed. Files (and saved settings) kept in $DIR – run with --purge to delete them."
fi
