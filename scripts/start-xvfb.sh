#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

RESOLUTION="${RESOLUTION:-1920x1080}"
# DISPLAY_SIZE (optional, .env): the virtual screen size, when it must differ from the broadcast RESOLUTION. Clean
# Broadcast's native mode uses it so Zoom's video area (screen minus Zoom's bars) is exactly the output size - e.g.
# 1928x1225 for a 1920x1080 broadcast. Unset or malformed = the screen is RESOLUTION, as before.
SCREEN="$RESOLUTION"
if [[ "${DISPLAY_SIZE:-}" =~ ^[0-9]{3,4}x[0-9]{3,4}$ ]]; then
  SCREEN="$DISPLAY_SIZE"
fi
log "Starting Xvfb on $DISPLAY_NUM at ${SCREEN}x24 (broadcast output ${RESOLUTION})"
exec /usr/bin/Xvfb "$DISPLAY_NUM" -screen 0 "${SCREEN}x24" -nolisten tcp -noreset
