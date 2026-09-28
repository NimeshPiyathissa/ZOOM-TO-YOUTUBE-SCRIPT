#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

tries=0
until pactl info >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [[ $tries -gt 60 ]]; then
    log "PipeWire/Pulse never became ready after 30s"
    exit 1
  fi
  sleep 0.5
done

ensure_null_sink() {
  local name="$1" channels="$2"
  if ! pactl list short sinks | awk '{print $2}' | grep -qx "$name"; then
    log "Creating null sink $name (48kHz, ${channels}ch)"
    pactl load-module module-null-sink "sink_name=$name" \
      "sink_properties=device.description=$name" rate=48000 "channels=$channels"
  else
    log "$name sink already exists"
  fi
}

# zoom_out: everything the bot *plays* (Zoom's speaker, Chrome's audio)
# lands here; stream.sh's ffmpeg captures zoom_out.monitor.
ensure_null_sink zoom_out 2

# bot_mic: a second, always-silent null sink whose monitor is the bot's
# *microphone*. There is no mic hardware on this VPS and with only
# zoom_out present PulseAudio's default source was zoom_out.monitor -
# i.e. Zoom's mic input was the loopback of the stream output, so an
# accidental unmute would have echoed the meeting back into itself.
# Pointing the default source at bot_mic.monitor gives Zoom a valid,
# selectable, silent mic: unmute is harmless, "Join Audio" works, and
# nothing is ever fed back. (To ever make the bot speak, play into the
# bot_mic sink: `paplay --device=bot_mic file.wav`.)
ensure_null_sink bot_mic 2

log "Setting zoom_out as the default output sink (Zoom's speaker output will land here)"
pactl set-default-sink zoom_out
log "Setting bot_mic.monitor as the default input source (Zoom's mic - silent, never the stream loopback)"
pactl set-default-source bot_mic.monitor

log "Audio setup complete"
