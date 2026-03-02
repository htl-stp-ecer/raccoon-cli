#!/bin/bash
# install.sh — Deploy raccoon server to Raspberry Pi from release tarball.
#
# Usage:
#   RPI_HOST=192.168.4.1 ./install.sh
#
# Env vars:
#   RPI_HOST  — Pi IP address (default: 192.168.4.1)
#   RPI_USER  — Pi SSH user   (default: pi)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOST="${RPI_HOST:-192.168.4.1}"
USER="${RPI_USER:-pi}"

# --- Preflight: check that wheels exist ---
TRANSPORT_WHL=$(find "$SCRIPT_DIR" -maxdepth 1 -name 'raccoon_transport-*.whl' | head -1)
RACCOON_WHL=$(find "$SCRIPT_DIR" -maxdepth 1 -name 'raccoon-*.whl' | head -1)

if [ -z "$TRANSPORT_WHL" ] || [ -z "$RACCOON_WHL" ]; then
    echo "ERROR: Expected raccoon_transport-*.whl and raccoon-*.whl in $SCRIPT_DIR"
    exit 1
fi

echo "Deploying to $USER@$HOST"
echo "  raccoon-transport: $(basename "$TRANSPORT_WHL")"
echo "  raccoon:           $(basename "$RACCOON_WHL")"

# --- Test SSH connection ---
echo "Testing SSH connection..."
ssh -o ConnectTimeout=5 "$USER@$HOST" true

# --- Stop service ---
echo "Stopping raccoon service..."
ssh "$USER@$HOST" 'sudo systemctl stop raccoon.service 2>/dev/null || true'

# --- Upload wheels ---
echo "Uploading wheels..."
REMOTE_TMP="/tmp/raccoon-install"
ssh "$USER@$HOST" "rm -rf $REMOTE_TMP && mkdir -p $REMOTE_TMP"
scp "$TRANSPORT_WHL" "$RACCOON_WHL" "$USER@$HOST:$REMOTE_TMP/"

# --- Install ---
echo "Installing..."
ssh "$USER@$HOST" "sudo pip3 install --break-system-packages --force-reinstall --no-deps $REMOTE_TMP/raccoon_transport-*.whl $REMOTE_TMP/raccoon-*.whl && sudo pip3 install --break-system-packages $REMOTE_TMP/raccoon-*.whl"

# --- Install & start systemd service ---
echo "Configuring systemd service..."
ssh "$USER@$HOST" 'sudo raccoon-server install'

# --- Ensure shell completion state exists ---
ssh "$USER@$HOST" 'if [ ! -f ~/.raccoon/cli_state.yml ]; then
  mkdir -p ~/.raccoon
  echo "completion_offered: true" > ~/.raccoon/cli_state.yml
else
  grep -q "^completion_offered:" ~/.raccoon/cli_state.yml || echo "completion_offered: true" >> ~/.raccoon/cli_state.yml
fi'

# --- Restart service ---
echo "Starting raccoon service..."
ssh "$USER@$HOST" 'sudo systemctl restart raccoon.service'

# --- Verify ---
echo ""
ssh "$USER@$HOST" 'systemctl is-active raccoon.service && raccoon-server status' || true
echo ""
echo "Deployment to $HOST completed."
