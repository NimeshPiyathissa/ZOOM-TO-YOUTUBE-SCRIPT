#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

log "Disabling screensaver/DPMS on $DISPLAY"
xset -display "$DISPLAY" s off
xset -display "$DISPLAY" s noblank
xset -display "$DISPLAY" -dpms

log "Starting Openbox on $DISPLAY"
exec /usr/bin/openbox
