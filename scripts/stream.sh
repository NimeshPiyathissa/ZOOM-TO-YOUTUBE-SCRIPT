#!/usr/bin/env bash
# The FFmpeg encode -> YouTube. Branches only on the *input* side by
# SOURCE_TYPE (from current-source.env, sourced by lib.sh):
#   zoom / webpage -> capture :99 (x11grab) + the zoom_out pulse sink,
#                     exactly as before the Source model existed.
#   direct         -> the source URL itself is ffmpeg's input; no Xvfb/
#                     Zoom/Chromium involved at all.
# The output side (x264/aac encode params, RTMP URL, restart policy, CPU
# priority) is one code path regardless, so it's never duplicated.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

# Direct-media sources never touch Xvfb, so don't block startup waiting
# for a display that nothing is going to use.
[[ "$SOURCE_TYPE" != "direct" ]] && wait_for_x

RESOLUTION="${RESOLUTION:-1920x1080}"
FPS="${FPS:-30}"
VIDEO_BITRATE="${VIDEO_BITRATE:-6000}"
AUDIO_BITRATE="${AUDIO_BITRATE:-192}"
X264_PRESET="${X264_PRESET:-veryfast}"
BUFSIZE=$((VIDEO_BITRATE * 2))
GOP=$((FPS * 2))

if [[ -z "${YT_STREAM_KEY:-}" ]]; then
  log "YT_STREAM_KEY is not set in .env - refusing to start"
  exit 1
fi

RTMP_URL="rtmps://a.rtmps.youtube.com:443/live2/${YT_STREAM_KEY}"
LOG_FILE="$LOG_DIR/ffmpeg.log"

# ---------------------------------------------------------------- input args

INPUT_ARGS=()

if [[ "$SOURCE_TYPE" == "direct" ]]; then
  if [[ -z "${DIRECT_URL:-}" ]]; then
    log "SOURCE_TYPE=direct but DIRECT_URL is empty - refusing to start"
    exit 1
  fi
  # Belt-and-braces: this must already have passed app/url_security.py
  # (both at save time and again immediately before current-source.env
  # was written) and must never start with '-'. Refuse to hand ffmpeg
  # anything that looks like a flag rather than a URL.
  case "$DIRECT_URL" in
    -*) log "DIRECT_URL looks like a flag, refusing: $DIRECT_URL"; exit 1 ;;
  esac

  if [[ "${DIRECT_LOOP:-0}" == "1" ]]; then
    INPUT_ARGS+=(-stream_loop -1)
  fi
  if [[ "${DIRECT_RECONNECT:-1}" == "1" ]]; then
    case "$DIRECT_URL" in
      http://*|https://*)
        INPUT_ARGS+=(-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5)
        ;;
    esac
  fi
  INPUT_ARGS+=(-thread_queue_size 1024 -i "$DIRECT_URL")
  WATERMARK_INPUT_IDX=1
  AUDIO_MAP="0:a"

  log "Starting FFmpeg (direct input, mode=${DIRECT_MODE:-reencode}): $DIRECT_URL"
else
  INPUT_ARGS=(
    -f x11grab -video_size "$RESOLUTION" -framerate "$FPS" -draw_mouse 0 \
      -thread_queue_size 1024 -use_wallclock_as_timestamps 1 -i "$DISPLAY"
    -f pulse -thread_queue_size 1024 -use_wallclock_as_timestamps 1 -i zoom_out.monitor
  )
  WATERMARK_INPUT_IDX=2
  AUDIO_MAP="1:a"
  log "Starting FFmpeg (capture, source=${SOURCE_TYPE}): ${RESOLUTION}@${FPS} v=${VIDEO_BITRATE}k a=${AUDIO_BITRATE}k preset=${X264_PRESET}"
fi

# Part 4: builds $WATERMARK_FILTER/$WATERMARK_EXTRA_INPUT_ARGS from the
# WATERMARK_* vars current-source.env already loaded (see lib.sh). Needs
# to know which input index the watermark image would occupy *before*
# deciding encode args below, since copy mode can't carry a burned-in
# filter - that decision depends on whether a watermark is actually active.
build_watermark_filter "$WATERMARK_INPUT_IDX"
if [[ -n "$WATERMARK_FILTER" && "$SOURCE_TYPE" == "direct" && "${DIRECT_MODE:-reencode}" == "copy" ]]; then
  # The dashboard is supposed to have already switched this source to
  # re-encode mode the moment the watermark was enabled (see
  # app/sources.py) - this is defense in depth against a stale/hand-edited
  # current-source.env, not the primary mechanism. A burned-in filter is
  # fundamentally incompatible with stream copy (no decode = nothing to
  # draw on), so this can't be silently ignored either way.
  log "Watermark is enabled but DIRECT_MODE=copy can't carry a burned-in filter - re-encoding this run instead"
fi

# ---------------------------------------------------------------- encode args

if [[ "$SOURCE_TYPE" == "direct" && "${DIRECT_MODE:-reencode}" == "copy" && -z "$WATERMARK_FILTER" ]]; then
  # Only ever offered by the dashboard when ffprobe confirmed
  # YouTube-compatible codecs (h264 + aac) - see app/probe.py.
  ENCODE_ARGS=(-c:v copy -c:a copy)
else
  AFILTER_ARGS=()
  [[ "$SOURCE_TYPE" != "direct" ]] && AFILTER_ARGS=(-af "aresample=async=1:first_pts=0")
  ENCODE_ARGS=(
    "${AFILTER_ARGS[@]}"
    -c:v libx264 -preset "$X264_PRESET" -pix_fmt yuv420p
      -g "$GOP" -keyint_min "$GOP" -sc_threshold 0
      -b:v "${VIDEO_BITRATE}k" -maxrate "${VIDEO_BITRATE}k" -bufsize "${BUFSIZE}k"
    -c:a aac -b:a "${AUDIO_BITRATE}k" -ar 48000 -ac 2
  )
fi

if [[ -n "$WATERMARK_FILTER" ]]; then
  INPUT_ARGS+=("${WATERMARK_EXTRA_INPUT_ARGS[@]}")
  if [[ "${WATERMARK_MODE:-text}" == "image" ]]; then
    # filter_complex disables ffmpeg's default stream auto-mapping, so
    # both the filtered video and the original audio need an explicit -map.
    ENCODE_ARGS+=(-filter_complex "$WATERMARK_FILTER" -map "[vout]" -map "$AUDIO_MAP")
  else
    ENCODE_ARGS+=(-vf "$WATERMARK_FILTER")
  fi
fi

# Plain redirection (not a pipe) so this process image IS ffmpeg after exec -
# systemd's SIGTERM on stop reaches ffmpeg directly for a clean shutdown,
# and Restart=always in ffmpeg-stream.service handles reconnecting if the
# RTMP connection to YouTube drops.
exec /usr/bin/ffmpeg -hide_banner -loglevel info -stats \
  "${INPUT_ARGS[@]}" \
  "${ENCODE_ARGS[@]}" \
  -f flv "$RTMP_URL" \
  >>"$LOG_FILE" 2>&1
