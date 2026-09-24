#!/usr/bin/env bash
# Switches the running x11vnc between two screen-poll rates without a
# restart, via x11vnc's own remote-control channel (an X property on
# :99 - `x11vnc -R`).
#
#   fast   poll every 16ms / defer 10ms  -> up to ~60 fps to the viewer
#          while the panel's Interact mode is on (snappy control).
#   slow   poll every 100ms / defer 20ms -> the start-vnc.sh default,
#          restored when the last VNC session closes.
#
# x11vnc runs at Nice=10 / CPUWeight=20 (systemd/x11vnc.service) and uses
# ~0.1% CPU even polling fast, so a faster poll can never take cycles the
# encoder (Nice=0, CPUWeight=800) wants; it only spends otherwise-idle CPU.
#
# Serialized with flock: the proxy fires this on every VNC connect/
# disconnect, and two instances racing on x11vnc's single -Q answer
# property was making the readback come back empty and the whole call
# "fail" - which left the poll stuck at the slow 100ms rate mid-session
# (the "remote screen is laggy" bug). The -R writes almost always land;
# the -Q readback is only best-effort verification now and never fails
# the call on its own.
set -uo pipefail
export DISPLAY="${DISPLAY:-:99}"

case "${1:-}" in
  fast) WAIT=16;  DEFER=10 ;;
  slow) WAIT=100; DEFER=20 ;;
  *) echo '{"ok": false, "error": "usage: set-vnc-rate.sh fast|slow"}'; exit 2 ;;
esac

exec 9>/tmp/.set-vnc-rate.lock
flock -w 3 9 || true   # if we can't get the lock in 3s, proceed anyway

apply() { x11vnc -R "$1" >/dev/null 2>&1; }
apply "wait:${WAIT}";  sleep 0.15
apply "defer:${DEFER}"; sleep 0.15

# Best-effort readback (retry a few times; another instance may be
# consuming the answer property). Never fail the call if it stays empty.
ANS=""
for _ in 1 2 3; do
  ANS="$(timeout 2 x11vnc -Q wait 2>/dev/null | grep -oE 'wait:[0-9]+' | cut -d: -f2)"
  [[ -n "$ANS" ]] && break
  sleep 0.15
done

echo "{\"ok\": true, \"mode\": \"$1\", \"wait_ms\": ${ANS:-$WAIT}, \"defer_ms\": ${DEFER}, \"verified\": $([[ -n "$ANS" ]] && echo true || echo false)}"
