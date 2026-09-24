#!/usr/bin/env bash
# ==============================================================================
# Local MP4 Stream Recording Script
# Records :99 and zoom_out.monitor to /home/zoombot/recordings/
# Enforces safety halt if free disk space falls below 2.0 GB (2,097,152 KB).
#
# Commands:
#   record-stream.sh start
#   record-stream.sh stop
#   record-stream.sh status
# ==============================================================================

set -uo pipefail

REC_DIR="/home/dashboard/recordings"
PID_FILE="$REC_DIR/.record.pid"
STATUS_FILE="$REC_DIR/.record.json"
MIN_FREE_KB=2097152 # 2.0 GB

mkdir -p "$REC_DIR"

check_free_kb() {
  local free_kb
  free_kb="$(df -k "$REC_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
  echo "${free_kb:-0}"
}

check_free_gb() {
  local free_kb
  free_kb="$(check_free_kb)"
  awk "BEGIN {printf \"%.2f\", $free_kb / 1048576}"
}

action_status() {
  local recording=false
  local pid=""
  local file=""
  local duration=0
  local size_mb=0
  local halted_reason=""
  local free_gb
  free_gb="$(check_free_gb)"

  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      recording=true
    else
      rm -f "$PID_FILE"
    fi
  fi

  if [[ -f "$STATUS_FILE" ]]; then
    # Read saved status metadata if present
    file="$(grep -oP '(?<="file": ")[^"]*' "$STATUS_FILE" 2>/dev/null || true)"
    halted_reason="$(grep -oP '(?<="halted_reason": ")[^"]*' "$STATUS_FILE" 2>/dev/null || true)"
    local start_ts
    start_ts="$(grep -oP '(?<="start_ts": )[0-9]+' "$STATUS_FILE" 2>/dev/null || true)"
    if [[ -n "$start_ts" ]]; then
      local now_ts
      now_ts="$(date +%s)"
      duration=$((now_ts - start_ts))
    fi
    if [[ -n "$file" && -f "$file" ]]; then
      local bytes
      bytes="$(stat -c %s "$file" 2>/dev/null || stat -f %z "$file" 2>/dev/null || echo 0)"
      size_mb="$(awk "BEGIN {printf \"%.2f\", $bytes / 1048576}")"
    fi
  fi

  cat <<EOF
{"recording": $recording, "file": "$file", "duration": $duration, "size_mb": $size_mb, "free_gb": $free_gb, "halted_reason": "$halted_reason"}
EOF
}

action_stop() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      # Send SIGINT so FFmpeg cleanly finalizes the MP4 header/moov atom
      kill -INT "$pid" 2>/dev/null || true
      # Grace period 2.0s (10 iterations of 0.2s)
      for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
      done
      # Force terminate any hung recording FFmpeg process after grace period
      if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$PID_FILE"
  fi
  action_status
}

action_start() {
  local free_kb
  free_kb="$(check_free_kb)"
  if (( free_kb < MIN_FREE_KB )); then
    echo "{\"error\": \"Insufficient disk space (< 2.0 GB free)\", \"recording\": false, \"free_gb\": $(check_free_gb)}" >&2
    exit 1
  fi

  # Stop any existing recording cleanly first
  if [[ -f "$PID_FILE" ]]; then
    action_stop >/dev/null 2>&1
  fi

  local ts
  ts="$(date +%Y%m%d_%H%M%S)"
  local start_epoch
  start_epoch="$(date +%s)"
  local out_file="$REC_DIR/rec_${ts}.mp4"

  local display="${DISPLAY:-:99}"
  local fps="${FPS:-30}"
  local resolution="${RESOLUTION:-}"
  if [[ -z "$resolution" ]]; then
    local detected_res
    detected_res="$(xdpyinfo -display "$display" 2>/dev/null | grep 'dimensions:' | awk '{print $2}')"
    resolution="${detected_res:-1280x720}"
  fi

  # Write initial status
  cat > "$STATUS_FILE" <<EOF
{"file": "$out_file", "start_ts": $start_epoch, "halted_reason": ""}
EOF

  # Launch recording ffmpeg in background
  /usr/bin/ffmpeg -nostdin -hide_banner -loglevel warning -y \
    -f x11grab -video_size "$resolution" -framerate "$fps" -draw_mouse 0 \
      -thread_queue_size 1024 -i "$display" \
    -f pulse -thread_queue_size 1024 -i zoom_out.monitor \
    -af "aresample=async=1:first_pts=0" \
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p \
    -c:a aac -b:a 192k -ar 48000 -ac 2 \
    "$out_file" </dev/null >"$REC_DIR/.record.log" 2>&1 &

  local ff_pid=$!
  echo "$ff_pid" > "$PID_FILE"

  # Background watcher to enforce safety halt if disk drops below 2.0 GB
  (
    while kill -0 "$ff_pid" 2>/dev/null; do
      sleep 5
      local cur_free
      cur_free="$(check_free_kb)"
      if (( cur_free < MIN_FREE_KB )); then
        # Safety halt: send SIGINT to finalize MP4 cleanly
        kill -INT "$ff_pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        cat > "$STATUS_FILE" <<EOF
{"file": "$out_file", "start_ts": $start_epoch, "halted_reason": "DISK_LOW_SAFETY_HALT"}
EOF
        break
      fi
    done
  ) </dev/null >/dev/null 2>&1 &
  disown -a 2>/dev/null || true

  action_status
}

case "${1:-status}" in
  start) action_start ;;
  stop)  action_stop ;;
  status) action_status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 1 ;;
esac
