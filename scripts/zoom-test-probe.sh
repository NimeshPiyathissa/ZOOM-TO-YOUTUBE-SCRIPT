#!/usr/bin/env bash
# One-shot, READ-ONLY probe for the real-meeting test: sudo bash zoom-test-probe.sh [--frame]
# Reports Zoom's meeting state, whether any encoder/broadcast is running, window geometry, the black borders around the
# picture (same rule as app/framing.py), a 2 s audio level on zoom_out.monitor, and the CPU/RAM snapshot.
# With --frame it also saves one still of the server screen to /tmp/probe-frame.png (deleted by the caller afterwards).
# Changes nothing, never prints the meeting link, passcode or stream key.
set -uo pipefail
ZUID=$(id -u zoombot); SCR=/home/zoombot/zoom-stream/scripts
zb() { sudo -u zoombot XDG_RUNTIME_DIR=/run/user/$ZUID DISPLAY=:99 "$@"; }
echo "== $(date -u +%H:%M:%S) UTC"
echo "-- Zoom"
zb python3 $SCR/zoom-status.py 2>/dev/null | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: print("  status unreadable"); sys.exit()
print("  status:", d.get("status"), "| attendee:", d.get("attendee"), "| detail:", (d.get("detail") or "")[:90])
print("  dialogs:", d.get("dialogs"))'
for q in mic camera; do printf "  %s: " $q; zb python3 $SCR/zoom-atspi.py $q 2>/dev/null | python3 -c 'import sys,json
try: d=json.load(sys.stdin); print(d.get("state"), "| available:", d.get("available"), "|", (d.get("reason") or d.get("name") or "")[:70])
except Exception: print("unreadable")'; done
echo "-- Services / safety"
echo "  zoom.service: $(zb systemctl --user is-active zoom.service) pid=$(zb systemctl --user show zoom.service -p MainPID --value) | ffmpeg-stream: $(zb systemctl --user is-active ffmpeg-stream.service) boot=$(zb systemctl --user is-enabled ffmpeg-stream.service 2>&1) | ffmpeg processes: $(pgrep -x ffmpeg | wc -l)"
echo "  dashboard viewers: $(ss -tn state established '( sport = :8000 )' | tail -n +2 | wc -l)"
echo "-- Windows on :99 (title not shown)"
zb wmctrl -lG 2>/dev/null | awk '{printf "  win %sx%s at %s,%s\n", $5, $6, $3, $4}'
echo "  screen: $(zb xdpyinfo 2>/dev/null | awk '/dimensions/{print $2}')"
echo "-- Black borders (pure black only; band = full width, y 56..H-96)"
SIZE=$(zb xdpyinfo 2>/dev/null | awk '/dimensions/{print $2}'); W=${SIZE%x*}; H=${SIZE#*x}
if [[ -n "$SIZE" ]]; then
  BH=$((H - 56 - 96))
  OUT=$(zb nice -n 19 ffmpeg -hide_banner -nostdin -loglevel info -f x11grab -video_size "$SIZE" -framerate 4 -draw_mouse 0 -i :99 -t 1.6 \
        -vf "crop=${W}:${BH}:0:56,cropdetect=limit=20:round=2" -f null - 2>&1 | grep -o 'crop=[0-9:]*' | tail -1)
  echo "  cropdetect: ${OUT:-none}  (band ${W}x${BH}; crop=w:h:x:y inside the band)"
  if [[ "$OUT" =~ crop=([0-9]+):([0-9]+):([0-9]+):([0-9]+) ]]; then
    w=${BASH_REMATCH[1]}; h=${BASH_REMATCH[2]}; x=${BASH_REMATCH[3]}; y=${BASH_REMATCH[4]}
    echo "  margins px: left=$x top=$y right=$((W - x - w)) bottom=$((BH - y - h))"
  fi
fi
echo "-- Audio on zoom_out.monitor (2 s)"
zb timeout 6 python3 $SCR/audio-level.py --seconds 2 2>/dev/null | tail -3 | python3 -c '
import sys, json
rows = [json.loads(l) for l in sys.stdin if l.strip().startswith("{")]
if rows: print("  peak %.1f dB  rms %.1f dB  (silence ~ -90 dB)" % (max(r["peak_db"] for r in rows), sum(r["rms_db"] for r in rows)/len(rows)))
else: print("  no audio samples")'
echo "-- Resources"
echo "  load $(cut -d' ' -f1-3 /proc/loadavg) | mem available $(free -m | awk 'NR==2{print $7}') MB"
ps -eo pcpu,rss,cmd --sort=-pcpu | grep -E "bin/ffmpeg|opt/zoom/zoom |uvicorn" | grep -v grep | awk '{printf "  cpu=%s%% rss=%dMB %s\n",$1,$2/1024,$3}' | cut -c1-70
if [[ "${1:-}" == "--frame" ]]; then
  zb nice -n 19 ffmpeg -hide_banner -nostdin -loglevel error -f x11grab -video_size "$SIZE" -draw_mouse 0 -i :99 -vframes 1 -y /tmp/probe-frame.png && chmod 644 /tmp/probe-frame.png && echo "frame saved: /tmp/probe-frame.png"
fi
