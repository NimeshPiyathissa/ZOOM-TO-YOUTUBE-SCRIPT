#!/usr/bin/env bash
# Read-only sampler for a real-meeting test: prints one line every INTERVAL seconds (default 5) with load, free memory,
# CPU/RSS of ffmpeg + Zoom + dashboard, and the encoder's own progress (fps, speed, dup, drop, bitrate).
# Changes nothing and never prints the stream URL/key (only ffmpeg's `frame=...` progress lines are read).
#   usage: sudo bash live-test-monitor.sh [seconds-between-samples] [samples]
INTERVAL="${1:-5}"; N="${2:-120}"
LOG=/home/zoombot/zoom-stream/logs/ffmpeg.log
printf "%-8s %-5s %-7s %-12s %-12s %-12s %-6s %-6s %-7s %-5s %-5s %-9s\n" time load1 memAvl ffmpeg_cpu%/MB zoom_cpu%/MB dash_cpu%/MB fps speed bitrate dup drop restarts
for ((i = 0; i < N; i++)); do
  ps_line() { ps -eo pcpu,rss,cmd | grep -E "$1" | grep -v grep | awk '{c+=$1; m+=$2} END {printf "%.0f/%d", c, m/1024}'; }
  prog=$(tail -c 4000 "$LOG" 2>/dev/null | tr '\r' '\n' | grep -E "^frame=" | tail -1)
  fps=$(sed -nE 's/.*fps= *([0-9.]+).*/\1/p' <<<"$prog"); spd=$(sed -nE 's/.*speed= *([0-9.]+)x.*/\1/p' <<<"$prog")
  br=$(sed -nE 's/.*bitrate= *([0-9.]+)kbits.*/\1/p' <<<"$prog"); dup=$(sed -nE 's/.*dup= *([0-9]+).*/\1/p' <<<"$prog"); drp=$(sed -nE 's/.*drop= *([0-9]+).*/\1/p' <<<"$prog")
  rs=$(sudo -u zoombot XDG_RUNTIME_DIR=/run/user/$(id -u zoombot) systemctl --user show ffmpeg-stream.service -p NRestarts --value 2>/dev/null)
  printf "%-8s %-5s %-7s %-12s %-12s %-12s %-6s %-6s %-7s %-5s %-5s %-9s\n" "$(date +%H:%M:%S)" "$(cut -d' ' -f1 /proc/loadavg)" \
    "$(free -m | awk 'NR==2{print $7}')" "$(ps_line 'bin/ffmpeg')" "$(ps_line 'opt/zoom/zoom ')" "$(ps_line 'uvicorn')" "${fps:--}" "${spd:--}" "${br:--}" "${dup:--}" "${drp:--}" "${rs:--}"
  sleep "$INTERVAL"
done
