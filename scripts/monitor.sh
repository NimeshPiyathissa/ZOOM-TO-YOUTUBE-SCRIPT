#!/usr/bin/env bash
# Quick health check: latest FFmpeg stats line (speed/fps/bitrate/drops),
# CPU/MEM per relevant process, and systemd --user unit status.
#
# Usage:
#   ./monitor.sh            one-shot snapshot
#   ./monitor.sh --watch 5  refresh every 5s (default interval 5s)

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

show_once() {
  echo "=== FFmpeg status ($(date '+%H:%M:%S')) ==="
  if [[ -f "$LOG_DIR/ffmpeg.log" ]]; then
    tr '\r' '\n' <"$LOG_DIR/ffmpeg.log" | grep -a 'frame=' | tail -n1 \
      || echo "(no stats line yet - just started?)"
  else
    echo "(no ffmpeg.log yet - is ffmpeg-stream running?)"
  fi
  echo
  echo "=== CPU / MEM per process ==="
  ps -C Xvfb,openbox,zoom,x11vnc,ffmpeg -o pid,comm,%cpu,%mem,etime --sort=-%cpu 2>/dev/null \
    || echo "(none of the tracked processes are running)"
  echo
  echo "=== systemd --user units ==="
  systemctl --user --no-pager --plain status xvfb openbox audio-setup zoom x11vnc ffmpeg-stream 2>/dev/null \
    | grep -E '\.service|Active:' || true
}

if [[ "${1:-}" == "--watch" ]]; then
  INTERVAL="${2:-5}"
  while true; do
    clear
    show_once
    sleep "$INTERVAL"
  done
else
  show_once
fi
