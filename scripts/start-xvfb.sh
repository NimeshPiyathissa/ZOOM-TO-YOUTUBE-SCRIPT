#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

RESOLUTION="${RESOLUTION:-1920x1080}"
log "Starting Xvfb on $DISPLAY_NUM at ${RESOLUTION}x24"
exec /usr/bin/Xvfb "$DISPLAY_NUM" -screen 0 "${RESOLUTION}x24" -nolisten tcp -noreset
