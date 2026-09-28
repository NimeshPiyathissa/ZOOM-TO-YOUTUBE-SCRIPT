#!/usr/bin/env bash
# ExecStartPre= for browser-source.service. Chrome's DevTools port
# (127.0.0.1:9222, see browser-source.sh) is how the dashboard controls
# the kiosk tab, so before launching a new Chrome make sure nothing else
# already owns that port:
#   - a leftover Chrome from a previous run of *ours* (same profile
#     directory prefix) is stopped so the new one can bind the port;
#   - anything else listening there is a hard failure - the dashboard
#     would otherwise end up driving a browser it didn't launch.
# Also confirms curl exists (browser-source.sh's readiness gate needs it).
# Waiting for the port itself is NOT done here: ExecStartPre runs before
# ExecStart, and Chrome (ExecStart) is what opens the port.
set -uo pipefail

PORT="${DEVTOOLS_PORT:-9222}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is missing - browser-source.sh needs it for the DevTools readiness check" >&2
  exit 1
fi

# ss -p only names processes we own; a foreign listener shows up with no
# pid= field, which is exactly the case we refuse.
LISTENERS="$(ss -ltnpH "sport = :${PORT}" 2>/dev/null || true)"
[[ -z "$LISTENERS" ]] && exit 0

PIDS="$(grep -oE 'pid=[0-9]+' <<<"$LISTENERS" | cut -d= -f2 | sort -u)"
if [[ -z "$PIDS" ]]; then
  echo "127.0.0.1:${PORT} is already held by a process that isn't ours - refusing to start Chrome" >&2
  exit 1
fi
for pid in $PIDS; do
  if tr '\0' ' ' </proc/"$pid"/cmdline 2>/dev/null | grep -q -- "--user-data-dir=${HOME}/.config/"; then
    echo "127.0.0.1:${PORT} is held by a leftover Chrome from a previous run (pid ${pid}); stopping it"
    kill "$pid" 2>/dev/null || true
  else
    echo "127.0.0.1:${PORT} is held by pid ${pid}, which isn't our kiosk Chrome - refusing to start" >&2
    exit 1
  fi
done
for _ in $(seq 1 20); do
  ss -ltnH "sport = :${PORT}" 2>/dev/null | grep -q . || exit 0
  sleep 0.5
done
echo "127.0.0.1:${PORT} still busy after stopping the leftover Chrome" >&2
exit 1
