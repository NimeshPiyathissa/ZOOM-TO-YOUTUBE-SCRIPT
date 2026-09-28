#!/usr/bin/env bash
# Mutes/unmutes or sets the volume of the zoom_out null sink that
# stream.sh's ffmpeg captures audio from (zoom_out.monitor) - this
# changes what viewers hear without ffmpeg or the RTMP connection ever
# being touched. Always prints back the real resulting state, read from
# pactl rather than assumed, so the caller never has to guess whether it
# actually took.
#
#   mute | unmute | status | volume <0-150>
# Output: "<muted|unmuted> <volume%> <sink state> <active input streams>"
#   e.g. "unmuted 100 RUNNING 1"   - something is playing into the sink
#        "unmuted 100 SUSPENDED 0" - nothing at all is playing, so a
#                                    silent meter means "no source",
#                                    not "broken pipeline"
set -euo pipefail

SINK="zoom_out"
ACTION="${1:-status}"

case "$ACTION" in
  mute)   pactl set-sink-mute "$SINK" 1 ;;
  unmute) pactl set-sink-mute "$SINK" 0 ;;
  status) ;;
  volume)
    VOL="${2:-}"
    if [[ ! "$VOL" =~ ^[0-9]{1,3}$ ]] || (( VOL > 150 )); then
      echo "volume must be 0-150" >&2; exit 1
    fi
    pactl set-sink-volume "$SINK" "${VOL}%"
    ;;
  *) echo "usage: $(basename "$0") mute|unmute|status|volume <0-150>" >&2; exit 1 ;;
esac

case "$(pactl get-sink-mute "$SINK")" in
  *": yes") MUTED="muted" ;;
  *": no")  MUTED="unmuted" ;;
  *) echo "unknown pactl output" >&2; exit 1 ;;
esac
# "Volume: front-left: 65536 / 100% / 0.00 dB, ..." -> first percentage
PCT="$(pactl get-sink-volume "$SINK" | grep -oE '[0-9]+%' | head -n1 | tr -d '%')"
# Sink state (RUNNING/IDLE/SUSPENDED) and how many apps are feeding it.
SINK_LINE="$(pactl list short sinks | awk -v s="$SINK" '$2==s {print; exit}')"
STATE="$(awk '{print $NF}' <<<"$SINK_LINE")"
SINK_IDX="$(awk '{print $1}' <<<"$SINK_LINE")"
STREAMS="$(pactl list short sink-inputs | awk -v i="$SINK_IDX" '$2==i' | wc -l | tr -d ' ')"
echo "$MUTED ${PCT:-100} ${STATE:-UNKNOWN} ${STREAMS:-0}"
