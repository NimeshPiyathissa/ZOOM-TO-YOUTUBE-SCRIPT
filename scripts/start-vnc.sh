#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

PASSWD_FILE="$HOME/.vnc/passwd"
if [[ ! -f "$PASSWD_FILE" ]]; then
  log "No VNC password file at $PASSWD_FILE (install.sh should have created it)"
  exit 1
fi

log "Starting x11vnc on 127.0.0.1:5900 (localhost only, ~10 updates/sec cap)"
exec /usr/bin/x11vnc \
  -display "$DISPLAY" \
  -rfbport 5900 \
  -localhost \
  -rfbauth "$PASSWD_FILE" \
  -forever -shared \
  -wait 100 \
  -desktop "zoom-stream-bot" \
  -quiet
