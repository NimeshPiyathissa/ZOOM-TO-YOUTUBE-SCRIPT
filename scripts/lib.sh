#!/usr/bin/env bash
# Shared helpers. Sourced by every other script in this directory.

# ffmpeg.log ends up containing the YouTube RTMP URL (stream key and all)
# in its normal startup output, so logs stay group-readable-only, never
# world-readable: only zoombot and (via group membership) the dashboard
# user can read them, and the dashboard always redacts before display.
umask 027

APP_DIR="${APP_DIR:-$HOME/zoom-stream}"
# ZOOMBOT_RUNTIME_ENV_FILE overrides where secrets are sourced from - set
# by the systemd unit once Part 0's vault migration (Phase A2) lands, to
# point at the tmpfs-backed file write-runtime-env.sh regenerates from the
# encrypted store (see dashboard's app/runtime_env.py). Unset (the default
# today), this is byte-for-byte the pre-vault behavior: read .env straight
# off persistent disk.
ENV_FILE="${ZOOMBOT_RUNTIME_ENV_FILE:-$APP_DIR/.env}"
SOURCE_ENV_FILE="$APP_DIR/current-source.env"
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
chmod 750 "$LOG_DIR"

# Loads KEY=VALUE lines from $1 and exports them, without ever passing the
# file through `source`/eval. Values come from the dashboard, so this must
# not let a stray space or shell metacharacter (&, $, ;, `, ...) in a value
# be re-interpreted as shell syntax - it only ever does a plain assignment.
# Supported line forms: KEY=value, KEY="value", KEY='value' (one matching
# pair of outer quotes is stripped; \" and \\ are unescaped inside double
# quotes). Blank lines and lines starting with # (after leading whitespace)
# are skipped. Any other line is treated as a fatal config error rather
# than silently ignored or executed.
load_env_file() {
  local file="$1" line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      if [[ "$val" == \"*\" && ${#val} -ge 2 ]]; then
        val="${val:1:${#val}-2}"
        val="${val//\\\"/\"}"
        val="${val//\\\\/\\}"
      elif [[ "$val" == \'*\' && ${#val} -ge 2 ]]; then
        val="${val:1:${#val}-2}"
      fi
      export "$key=$val"
    else
      echo "Malformed line in $file, refusing to load: $line" >&2
      exit 1
    fi
  done < "$file"
}

if [[ -f "$ENV_FILE" ]]; then
  load_env_file "$ENV_FILE"
else
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

# current-source.env (Change 1) - which source is active and its
# type-specific options (URL, playback flags). Not secret, so unlike
# .env it's fine for this to be silently absent on an older install that
# hasn't picked a source yet; SOURCE_TYPE then defaults to "zoom" below,
# matching pre-Source-model behavior.
if [[ -f "$SOURCE_ENV_FILE" ]]; then
  load_env_file "$SOURCE_ENV_FILE"
fi
SOURCE_TYPE="${SOURCE_TYPE:-zoom}"

DISPLAY_NUM="${DISPLAY_NUM:-:99}"
export DISPLAY="${DISPLAY:-$DISPLAY_NUM}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ------------------------------------------------------------ Part 4: watermark
#
# The real fix for the watermark feature: previously the dashboard only
# injected a DOM overlay into whatever Chrome tab happened to be open
# (app/overlay.py's old CDP approach) - which the encoder never saw at
# all (x11grab captures raw framebuffer pixels, not the DOM) and which a
# Zoom-type source couldn't have worked with even in principle (no Chrome
# tab exists for it to attach to). This burns the watermark into the
# encoder's own filter graph instead, so it's real for every source type.
#
# Reads WATERMARK_* from current-source.env (already loaded above by
# load_env_file - not secret, same file webpage/direct URLs live in).
# Called by both stream.sh (the real broadcast) and test-recording.sh
# (the safe local-file verification path) - one code path, never
# duplicated, matching this project's existing convention for the rest of
# the encode/output side.
#
# Sets two globals: WATERMARK_FILTER (empty if disabled/misconfigured) and
# WATERMARK_EXTRA_INPUT_ARGS (an array - the image file as a second -i,
# only in image mode).
FONTS_DIR="$APP_DIR/scripts/fonts"

_watermark_anchor_xy() {
  # $1 = anchor name, $2 = margin_x, $3 = margin_y, $4 = "text"|"image"
  # text mode uses drawtext's w/h (video) and text_w/text_h (rendered text);
  # image mode uses overlay's W/H (base video) and w/h (overlay image).
  local anchor="$1" mx="$2" my="$3" kind="$4"
  local vw vh ow oh
  if [[ "$kind" == "image" ]]; then vw="W"; vh="H"; ow="w"; oh="h"
  else vw="w"; vh="h"; ow="text_w"; oh="text_h"; fi
  case "$anchor" in
    top-left)      echo "x=${mx}:y=${my}" ;;
    top-center)    echo "x=(${vw}-${ow})/2:y=${my}" ;;
    top-right)     echo "x=${vw}-${ow}-${mx}:y=${my}" ;;
    middle-left)   echo "x=${mx}:y=(${vh}-${oh})/2" ;;
    center)        echo "x=(${vw}-${ow})/2:y=(${vh}-${oh})/2" ;;
    middle-right)  echo "x=${vw}-${ow}-${mx}:y=(${vh}-${oh})/2" ;;
    bottom-left)   echo "x=${mx}:y=${vh}-${oh}-${my}" ;;
    bottom-center) echo "x=(${vw}-${ow})/2:y=${vh}-${oh}-${my}" ;;
    *)             echo "x=${vw}-${ow}-${mx}:y=${vh}-${oh}-${my}" ;;  # bottom-right, and default
  esac
}

_watermark_alpha() {
  # opacity 0-100 -> "0.00".."1.00", clamped, defaulting to fully opaque
  # on anything unparseable rather than failing the whole encode over a
  # cosmetic setting.
  awk -v o="${1:-100}" 'BEGIN {
    if (o !~ /^[0-9]+(\.[0-9]+)?$/) o = 100
    if (o < 0) o = 0; if (o > 100) o = 100
    printf "%.2f", o / 100
  }'
}

build_watermark_filter() {
  # $1 = the ffmpeg input index the watermark image will occupy, i.e. how
  # many -i inputs the caller has already built (x11grab+pulse = 2 for a
  # capture source, 1 for a direct source) - required because
  # image-mode's filter_complex graph references it by index ([N:v]), and
  # that index depends on which source type is active, not a fixed value.
  # Unused/ignored in text mode.
  local watermark_input_idx="${1:-1}"
  WATERMARK_FILTER=""
  WATERMARK_EXTRA_INPUT_ARGS=()

  if [[ "${WATERMARK_ENABLED:-0}" != "1" ]]; then
    return 0
  fi

  local anchor="${WATERMARK_ANCHOR:-bottom-right}"
  local margin_x="${WATERMARK_MARGIN_X:-24}"
  local margin_y="${WATERMARK_MARGIN_Y:-24}"
  local opacity_pct="${WATERMARK_OPACITY:-100}"
  local alpha; alpha="$(_watermark_alpha "$opacity_pct")"

  if [[ "${WATERMARK_MODE:-text}" == "image" ]]; then
    if [[ -z "${WATERMARK_IMAGE_PATH:-}" || ! -f "${WATERMARK_IMAGE_PATH}" ]]; then
      log "Watermark: image mode enabled but WATERMARK_IMAGE_PATH is missing or unreadable - skipping"
      return 0
    fi
    local scale_pct="${WATERMARK_SIZE:-15}"
    local xy; xy="$(_watermark_anchor_xy "$anchor" "$margin_x" "$margin_y" "image")"
    WATERMARK_EXTRA_INPUT_ARGS=(-i "${WATERMARK_IMAGE_PATH}")
    # scale2ref sizes the watermark relative to the *main* video's width
    # (scale_pct% of it) regardless of the source image's own resolution,
    # so the same uploaded logo looks the same size at 720p or 1080p.
    # Explicit [vout] label + the caller's own -map, since filter_complex
    # (unlike -vf) doesn't auto-select an output stream.
    WATERMARK_FILTER="[${watermark_input_idx}:v][0:v]scale2ref=w=iw*${scale_pct}/100:h=ow/mdar[wm][base];[wm]format=rgba,colorchannelmixer=aa=${alpha}[wma];[base][wma]overlay=${xy}[vout]"
  else
    local text="${WATERMARK_TEXT:-}"
    if [[ -z "$text" ]]; then
      log "Watermark: text mode enabled but WATERMARK_TEXT is empty - skipping"
      return 0
    fi
    local font_key="${WATERMARK_FONT:-inter}"
    local font_file
    case "$font_key" in
      jetbrains-mono) font_file="$FONTS_DIR/JetBrainsMono.ttf" ;;
      *)              font_file="$FONTS_DIR/Inter-Variable.ttf" ;;
    esac
    if [[ ! -f "$font_file" ]]; then
      log "Watermark: font file missing ($font_file) - skipping"
      return 0
    fi
    local size="${WATERMARK_SIZE:-28}"
    local xy; xy="$(_watermark_anchor_xy "$anchor" "$margin_x" "$margin_y" "text")"
    # textfile= (not text=) deliberately - sidesteps drawtext's own
    # argument-string escaping (colons, quotes, percent signs, backslashes
    # are all special to it) entirely for arbitrary operator-supplied
    # text, a standard ffmpeg technique for exactly this problem.
    local textfile="$LOG_DIR/.watermark-text.$$"
    printf '%s' "$text" > "$textfile"
    trap 'rm -f "'"$textfile"'"' EXIT
    WATERMARK_FILTER="drawtext=fontfile=${font_file}:textfile=${textfile}:fontsize=${size}:fontcolor=white@${alpha}:${xy}"
  fi
}

wait_for_x() {
  local tries=0
  until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [[ $tries -gt 60 ]]; then
      log "X display $DISPLAY never came up after 30s"
      exit 1
    fi
    sleep 0.5
  done
}

# "Meeting only" encoder filter -> $MEETING_ONLY_FILTER (empty when off).
# Args: out_w out_h fit(fit|fill) trim_top trim_bottom [region x:y:w:h].
#
# Zoom keeps drawing its own chrome around the video (top bar with the
# meeting title/REC/Sign in, the bottom toolbar, the side-panel arrow),
# and neither fullscreen nor parking the pointer reliably removes all of
# it, so the top/bottom bands are cut out here - the one place that is
# guaranteed to be hidden in the output. What is left is the meeting
# area, and it is never stretched:
#   fit  - the whole meeting area, scaled to fit, letterboxed with the
#          smallest bars that preserves its shape;
#   fill - the smallest centred crop that makes the meeting area exactly
#          the output shape (no bars), then scaled up.
# `region` (optional) is a MEASURED meeting area (app/framing.py: the
# part of the screen that is not pure-black border). When it is sane it
# replaces the fixed top/bottom band; when it is missing or implausible
# (smaller than a third of the frame, outside it, not numbers) the fixed
# trims are used - a bad measurement can never crop real content away.
# All sizes are rounded to even numbers (yuv420p needs that).
build_meeting_only_filter() {
  local out_w="$1" out_h="$2" fit="${3:-fit}" top="${4:-56}" bottom="${5:-96}" region="${6:-}"
  MEETING_ONLY_FILTER=""
  [[ "$out_w" =~ ^[0-9]+$ && "$out_h" =~ ^[0-9]+$ ]] || return 0
  [[ "$top" =~ ^[0-9]+$ ]] || top=56
  [[ "$bottom" =~ ^[0-9]+$ ]] || bottom=96
  # Never trim away more than a sixth of the frame per edge, whatever was configured.
  local max=$((out_h / 6))
  (( top > max )) && top=$max
  (( bottom > max )) && bottom=$max
  top=$((top / 2 * 2)); bottom=$((bottom / 2 * 2))
  local rx=0 ry=$top rw=$out_w rh=$((out_h - top - bottom))
  if [[ "$region" =~ ^([0-9]+):([0-9]+):([0-9]+):([0-9]+)$ ]]; then
    local mx=$((${BASH_REMATCH[1]} / 2 * 2)) my=$((${BASH_REMATCH[2]} / 2 * 2))
    local mw=$((${BASH_REMATCH[3]} / 2 * 2)) mh=$((${BASH_REMATCH[4]} / 2 * 2))
    if (( mw * 3 >= out_w && mh * 3 >= out_h && mx + mw <= out_w && my + mh <= out_h )); then
      rx=$mx; ry=$my; rw=$mw; rh=$mh
    fi
  fi
  rh=$((rh / 2 * 2))
  if [[ "$fit" == "fill" ]]; then
    local cw ch
    if (( rw * out_h > rh * out_w )); then   # meeting area wider than output: trim the sides
      ch=$rh; cw=$((rh * out_w / out_h))
    else                                     # taller: trim top/bottom
      cw=$rw; ch=$((rw * out_h / out_w))
    fi
    cw=$((cw / 2 * 2)); ch=$((ch / 2 * 2))
    local cx=$(( rx + (rw - cw) / 2 / 2 * 2 )) cy=$(( ry + (rh - ch) / 2 / 2 * 2 ))
    MEETING_ONLY_FILTER="crop=${cw}:${ch}:${cx}:${cy},scale=${out_w}:${out_h}:flags=lanczos,setsar=1"
  else
    MEETING_ONLY_FILTER="crop=${rw}:${rh}:${rx}:${ry},scale=${out_w}:${out_h}:force_original_aspect_ratio=decrease:flags=lanczos,pad=${out_w}:${out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
  fi
}

# "Fullscreen Live" framing (dashboard app/fullscreen_live.py decides the box; its fullscreen_filter() is this
# function's Python twin, pinned to identical strings by tests/test_fullscreen_live.py). The box is the smallest
# output-shaped area containing all non-black content, so the filter only ever removes black and scales uniformly.
# An unusable box falls back to the whole capture - this can never crop content. "" = no filter (native 1:1).
#   args: capture_w capture_h out_w out_h box(x:y:w:h)  ->  $FULLSCREEN_LIVE_FILTER
build_fullscreen_live_filter() {
  local W="$1" H="$2" ow="$3" oh="$4" box="${5:-}" x=0 y=0 w h crop=""
  FULLSCREEN_LIVE_FILTER=""
  [[ "$W" =~ ^[0-9]+$ && "$H" =~ ^[0-9]+$ && "$ow" =~ ^[0-9]+$ && "$oh" =~ ^[0-9]+$ ]] || return 0
  w=$W; h=$H
  if [[ "$box" =~ ^([0-9]+):([0-9]+):([0-9]+):([0-9]+)$ ]]; then
    local bx=$((10#${BASH_REMATCH[1]})) by=$((10#${BASH_REMATCH[2]})) bw=$((10#${BASH_REMATCH[3]})) bh=$((10#${BASH_REMATCH[4]}))
    if (( bw > 0 && bh > 0 && bx + bw <= W && by + bh <= H )); then
      x=$bx; y=$by; w=$bw; h=$bh
    fi
  fi
  if (( x != 0 || y != 0 || w != W || h != H )); then
    crop="crop=${w}:${h}:${x}:${y},"
  elif (( W == ow && H == oh )); then
    return 0
  fi
  local d=$(( w * oh - h * ow )); (( d < 0 )) && d=$(( -d ))
  if (( d * 1000 <= h * ow )); then                  # same shape to within 0.1% (sub-pixel): one uniform scale
    FULLSCREEN_LIVE_FILTER="${crop}scale=${ow}:${oh}:flags=lanczos,setsar=1"
  else
    FULLSCREEN_LIVE_FILTER="${crop}scale=${ow}:${oh}:force_original_aspect_ratio=decrease:flags=lanczos,pad=${ow}:${oh}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
  fi
}

# Capture size + framing filter for the x11grab capture, shared by stream.sh and test-recording.sh so a local test
# recording is framed exactly like the broadcast. Reads SOURCE_TYPE and the ZOOM_* framing keys of current-source.env.
#   args: output_size(WxH)  ->  $CAPTURE_SIZE, $CROP_FILTER ("" = none), $FRAMING_NOTE (for the log)
build_capture_framing() {
  local res="${1:-1920x1080}"
  CAPTURE_SIZE="$res"; CROP_FILTER=""; FRAMING_NOTE=""
  if [[ "${SOURCE_TYPE:-}" == "zoom" && "${ZOOM_FULLSCREEN_LIVE:-0}" == "1" ]]; then
    # Fullscreen Live replaces Meeting-only: capture the WHOLE desktop the dashboard measured (a capture smaller than
    # the screen grabs only its top-left corner), then the smallest 16:9 box around all content, uniform scale, no trims.
    [[ "${ZOOM_CAPTURE_SIZE:-}" =~ ^[0-9]+x[0-9]+$ ]] && CAPTURE_SIZE="$ZOOM_CAPTURE_SIZE"
    build_fullscreen_live_filter "${CAPTURE_SIZE%x*}" "${CAPTURE_SIZE#*x}" "${res%x*}" "${res#*x}" "${ZOOM_FRAMING_BOX:-}"
    CROP_FILTER="$FULLSCREEN_LIVE_FILTER"
    FRAMING_NOTE="Fullscreen Live (box ${ZOOM_FRAMING_BOX:-whole screen}): ${CROP_FILTER:-no filter, native ${CAPTURE_SIZE}}"
  elif [[ "${SOURCE_TYPE:-}" == "zoom" && "${ZOOM_MEETING_ONLY:-0}" == "1" ]]; then
    # "Meeting only": cut Zoom's own top/bottom chrome and fit or fill what is left (build_meeting_only_filter).
    build_meeting_only_filter "${res%x*}" "${res#*x}" "${ZOOM_MEETING_ONLY_FIT:-fit}" "${ZOOM_MEETING_ONLY_TRIM_TOP:-56}" "${ZOOM_MEETING_ONLY_TRIM_BOTTOM:-96}" "${ZOOM_MEETING_ONLY_REGION:-}"
    CROP_FILTER="$MEETING_ONLY_FILTER"
    FRAMING_NOTE="Meeting-only (${ZOOM_MEETING_ONLY_FIT:-fit}${ZOOM_MEETING_ONLY_REGION:+, measured area ${ZOOM_MEETING_ONLY_REGION}}): ${CROP_FILTER}"
  fi
  return 0
}

# ------------------------------------------------------------ broadcast authorization + secret-safe logging
#
# The encoder may only start when the dashboard has just written a ONE-TIME permission into current-source.env
# (BROADCAST_AUTH_NONCE / _EXPIRES, see dashboard app/broadcast.py) after an operator confirmed Go Live. This is the
# last line of defence: it stops a direct `systemctl --user start ffmpeg-stream`, systemd's own Restart= policy after a
# crash, a boot, a stale dashboard and every code path that might one day call "start" from starting a broadcast.
# The permission is burned (blanked on disk, under a lock) before ffmpeg starts, so it works exactly once.
#   return 0 = authorized (and burned), 1 = refuse.
broadcast_gate() {
  local nonce="${BROADCAST_AUTH_NONCE:-}" exp="${BROADCAST_AUTH_EXPIRES:-0}" now lock tries=0
  now="$(date +%s)"
  [[ -n "$nonce" && "$exp" =~ ^[0-9]+$ ]] || return 1
  (( now <= exp )) || return 1
  [[ -f "$SOURCE_ENV_FILE" ]] || return 1
  # mkdir is atomic everywhere (no flock dependency): whoever creates the directory holds the lock.
  lock="$SOURCE_ENV_FILE.lockdir"
  until mkdir "$lock" 2>/dev/null; do
    tries=$((tries + 1))
    (( tries > 50 )) && return 1                                   # 5 s: refuse rather than start unauthorized
    if [[ -d "$lock" && -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]]; then rmdir "$lock" 2>/dev/null; fi
    sleep 0.1
  done
  local ok=1 tmp="$SOURCE_ENV_FILE.burn.$$"
  # Re-read under the lock: if another instance already burned this nonce, refuse (single use, no double start).
  if grep -q "^BROADCAST_AUTH_NONCE=\"$nonce\"\$" "$SOURCE_ENV_FILE"; then
    if sed -E 's/^(BROADCAST_AUTH_(ID|NONCE|EXPIRES))=.*/\1=""/' "$SOURCE_ENV_FILE" > "$tmp" \
       && chmod 644 "$tmp" && mv -f "$tmp" "$SOURCE_ENV_FILE"; then
      ok=0
    fi
  fi
  rm -f "$tmp" 2>/dev/null
  rmdir "$lock" 2>/dev/null
  (( ok == 0 )) || return 1
  unset BROADCAST_AUTH_NONCE BROADCAST_AUTH_EXPIRES BROADCAST_AUTH_ID
  return 0
}

# Filter for ffmpeg's log output: masks the secret path part of any rtmp(s):// destination (the YouTube stream key).
# Never prints or stores the real value.
# ffmpeg ends its live progress line with \r, not \n, so a line-based filter (sed -u) would hold every progress update
# until the next newline - often until the encoder exits - and ffmpeg.log would look frozen while the stream is healthy
# (no fps/bitrate/dropped-frame metrics on the dashboard). perl (perl-base is part of every Ubuntu install) splits on \r
# instead: each progress update, and any \n lines before it, is written out as soon as it arrives, otherwise unchanged.
redact_stream_secrets() {
  if command -v perl >/dev/null 2>&1; then
    perl -e '$| = 1; $/ = "\r"; while (<STDIN>) { s#(rtmps?://[^/\s\x27"]+/[^/\s\x27"]+/)[^\s\x27"]+#${1}<redacted>#g; print }'
  else
    # fallback: turn \r into \n so sed sees whole lines (progress then lands one per line; the dashboard parses both)
    stdbuf -o0 tr '\r' '\n' | sed -u -E "s#(rtmps?://[^/[:space:]'\"]+/[^/[:space:]'\"]+/)[^[:space:]'\"]+#\1<redacted>#g"
  fi
}
