#!/usr/bin/env bash
# Convenience wrapper around the systemd --user units. Run this as the
# zoombot user, e.g.:
#   sudo -u zoombot XDG_RUNTIME_DIR=/run/user/$(id -u zoombot) ~/zoom-stream/scripts/manage.sh up
#
# Subcommands:
#   up          start display + audio + zoom + vnc (NOT the stream yet)
#   go-live     start ffmpeg-stream (call this after verifying over VNC)
#   down        stop everything, in the right order
#   status      show all unit statuses
#   logs [unit] follow logs for one unit (default: ffmpeg-stream)
#   restart <unit>

set -euo pipefail

if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi

CORE_UNITS=(xvfb.service openbox.service audio-setup.service zoom.service x11vnc.service)
ALL_UNITS=(xvfb.service openbox.service audio-setup.service zoom.service x11vnc.service ffmpeg-stream.service)

cmd="${1:-}"
[[ $# -gt 0 ]] && shift

case "$cmd" in
  up)
    echo "Starting display, audio, Zoom, and VNC (stream not started yet)..."
    systemctl --user start "${CORE_UNITS[@]}"
    echo "Connect over the SSH+VNC tunnel to verify the meeting looks right,"
    echo "then run: $0 go-live"
    ;;
  go-live)
    echo "Starting ffmpeg-stream..."
    systemctl --user start ffmpeg-stream.service
    ;;
  down)
    echo "Stopping everything..."
    systemctl --user stop ffmpeg-stream.service zoom.service x11vnc.service \
      audio-setup.service openbox.service xvfb.service
    ;;
  status)
    systemctl --user --no-pager status "${ALL_UNITS[@]}"
    ;;
  logs)
    unit="${1:-ffmpeg-stream}"
    journalctl --user -u "${unit}.service" -f -n 100
    ;;
  restart)
    unit="${1:?usage: $0 restart <unit>}"
    systemctl --user restart "${unit}.service"
    ;;
  *)
    echo "Usage: $0 {up|go-live|down|status|logs [unit]|restart <unit>}"
    exit 1
    ;;
esac
