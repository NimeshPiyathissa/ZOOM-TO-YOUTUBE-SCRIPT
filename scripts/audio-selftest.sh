#!/usr/bin/env bash
# End-to-end proof that audio reaches the stream: plays a known 440 Hz
# tone into the zoom_out sink, watches the same level meter the panel
# shows (audio-level.py on zoom_out.monitor), records a few seconds
# through the *identical* capture graph stream.sh/test-recording.sh use
# (x11grab + pulse zoom_out.monitor -> x264/aac), then measures the
# recorded file with ffmpeg's volumedetect. If the meter moves and the
# file's mean volume is well above silence, the sink -> encoder path is
# proven; a silent panel meter after that means nothing is playing, not
# a broken pipeline.
#
# Refuses to run while ffmpeg-stream is active (viewers would hear the
# tone) unless --force is given. Prints one JSON line.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

FORCE=0; [[ "${1:-}" == "--force" ]] && FORCE=1
if [[ "$FORCE" != 1 ]] && systemctl --user is-active --quiet ffmpeg-stream.service; then
  echo '{"ok": false, "error": "ffmpeg-stream is live - the test tone would go out to viewers. Stop the stream first (or pass --force)."}'
  exit 1
fi

DUR=6
TONE="$LOG_DIR/audio-selftest-tone.wav"
OUT="$LOG_DIR/audio-selftest.mp4"
RESOLUTION="${RESOLUTION:-1280x720}"

ffmpeg -hide_banner -loglevel error -y -f lavfi -i "sine=frequency=440:sample_rate=48000" -t $((DUR + 3)) -ac 2 -c:a pcm_s16le "$TONE" \
  || { echo '{"ok": false, "error": "could not synthesize the test tone"}'; exit 1; }

paplay --device=zoom_out "$TONE" &
TONE_PID=$!
sleep 1

METER="$(python3 ./audio-level.py --seconds 2 2>/dev/null | tail -n1)"
PEAK="$(grep -oE '"peak_db": *-?[0-9.]+' <<<"$METER" | grep -oE -- '-?[0-9.]+$')"

ffmpeg -hide_banner -loglevel error -y \
  -f x11grab -video_size "$RESOLUTION" -framerate 30 -draw_mouse 0 -thread_queue_size 1024 -i "$DISPLAY" \
  -f pulse -thread_queue_size 1024 -i zoom_out.monitor \
  -af "aresample=async=1:first_pts=0" -t "$DUR" \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -b:a 160k -ar 48000 -ac 2 "$OUT"
REC_RC=$?
wait "$TONE_PID" 2>/dev/null
rm -f "$TONE"

if [[ $REC_RC -ne 0 || ! -s "$OUT" ]]; then
  echo "{\"ok\": false, \"error\": \"capture recording failed (rc=$REC_RC)\", \"meter_peak_db\": ${PEAK:-null}}"
  exit 1
fi

STATS="$(ffmpeg -hide_banner -nostats -i "$OUT" -vn -af volumedetect -f null - 2>&1)"
MEAN="$(grep -oE 'mean_volume: *-?[0-9.]+' <<<"$STATS" | grep -oE -- '-?[0-9.]+$')"
MAX="$(grep -oE 'max_volume: *-?[0-9.]+' <<<"$STATS" | grep -oE -- '-?[0-9.]+$')"
HAS_AUDIO="$(ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 "$OUT" | head -n1)"

OK=false
if [[ -n "$MEAN" && -n "$PEAK" ]] && awk -v m="$MEAN" -v p="$PEAK" 'BEGIN{exit !(m > -40 && p > -40)}'; then
  OK=true
fi
echo "{\"ok\": $OK, \"tone_hz\": 440, \"seconds\": $DUR, \"meter_peak_db\": ${PEAK:-null}, \"file\": \"$OUT\", \"file_audio_codec\": \"${HAS_AUDIO:-none}\", \"file_mean_volume_db\": ${MEAN:-null}, \"file_max_volume_db\": ${MAX:-null}}"
