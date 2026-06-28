#!/bin/bash
# Install the Batocera Toolbox onto a Batocera machine over SSH.
#
#   ./install.sh <batocera-ip> [ssh-user]
#
# Example:
#   ./install.sh 192.168.1.50
#
# Batocera ships Python 3, pygame, and rsync, so there are no extra deps.
# The Toolbox appears in the PORTS menu after install.
set -euo pipefail

IP="${1:-}"
USER="${2:-root}"
if [ -z "$IP" ]; then
  echo "usage: $0 <batocera-ip> [ssh-user]" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$USER@$IP"
PORTS="/userdata/roms/ports"

echo "Installing Batocera Toolbox to $TARGET:$PORTS ..."
rsync -a --delete --exclude='__pycache__/' -e ssh "$HERE/toolbox/" "$TARGET:$PORTS/toolbox/"
scp "$HERE/toolbox.sh" "$TARGET:$PORTS/Toolbox.sh"
ssh "$TARGET" 'chmod +x /userdata/roms/ports/Toolbox.sh; curl -s http://127.0.0.1:1234/reloadgames >/dev/null 2>&1 || true'

echo "Done. Look for 'Toolbox' in the PORTS menu (you may need to refresh the gamelist)."
