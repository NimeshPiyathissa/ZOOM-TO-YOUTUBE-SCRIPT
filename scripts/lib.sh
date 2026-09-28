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
