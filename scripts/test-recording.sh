#!/usr/bin/env bash
# 60-second local test: same capture pipeline as stream.sh but writes an
# .mp4 instead of pushing to YouTube. Use this to check video, audio
# level, and A/V sync before ever going live.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

RESOLUTION="${RESOLUTION:-1920x1080}"
FPS="${FPS:-30}"
AUDIO_BITRATE="${AUDIO_BITRATE:-192}"
DURATION="${TEST_DURATION:-60}"
OUT="$LOG_DIR/test-$(date +%Y%m%d-%H%M%S).mp4"

# Part 4: same shared filter-building call stream.sh makes, so this is a
# genuine proof of what the real broadcast would do with the current
# watermark config - not a separate, potentially-drifted copy of the
# logic. Always capture mode here (x11grab=0, pulse=1), so the watermark
# image (if any) would be input index 2.
build_watermark_filter 2

ENCODE_ARGS=(-c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -b:a "${AUDIO_BITRATE}k" -ar 48000 -ac 2)
INPUT_ARGS=(
  -f x11grab -video_size "$RESOLUTION" -framerate "$FPS" -draw_mouse 0 -thread_queue_size 1024 -i "$DISPLAY"
  -f pulse -thread_queue_size 1024 -i zoom_out.monitor
)
if [[ -n "$WATERMARK_FILTER" ]]; then
  INPUT_ARGS+=("${WATERMARK_EXTRA_INPUT_ARGS[@]}")
  if [[ "${WATERMARK_MODE:-text}" == "image" ]]; then
    ENCODE_ARGS+=(-filter_complex "$WATERMARK_FILTER" -map "[vout]" -map "1:a")
  else
    ENCODE_ARGS+=(-vf "$WATERMARK_FILTER")
  fi
else
  ENCODE_ARGS+=(-af "aresample=async=1:first_pts=0")
fi

log "Recording ${DURATION}s local test to $OUT (watermark: ${WATERMARK_ENABLED:-0})"
/usr/bin/ffmpeg -hide_banner -loglevel info -stats -y \
  "${INPUT_ARGS[@]}" \
  -t "$DURATION" \
  "${ENCODE_ARGS[@]}" \
  "$OUT"

ln -sf "$(basename "$OUT")" "$LOG_DIR/latest-test.mp4"

log "Done: $OUT"
log "Copy it off the VPS to review, e.g. from your own machine:"
log "  scp <ssh-user>@<vps-ip>:$OUT ."
