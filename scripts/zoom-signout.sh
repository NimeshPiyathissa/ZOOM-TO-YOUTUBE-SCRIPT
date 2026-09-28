#!/usr/bin/env bash
# Best-effort Zoom sign-out: stops any running Zoom session, then removes
# the local session-data directory so the client shows a fresh sign-in
# screen next launch. Does not touch the Chromium profile (shared with
# webpage sources - it has its own separate site-data story if that's
# ever needed) and never reads/logs anything from the removed files
# before deleting them.
#
# NOTE: like zoom-google-signin.sh, the exact path Zoom stores its
# session under can vary by client version; this targets the path this
# project's control.py already treats as the sign-in heuristic
# (~/.zoom/data). Confirmed once against the real installed version -
# see the deployment notes.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

if pgrep -u "$(id -u)" -x zoom >/dev/null 2>&1; then
  log "Stopping running Zoom process before sign-out"
  pkill -u "$(id -u)" -x zoom || true
  for _ in $(seq 1 10); do
    pgrep -u "$(id -u)" -x zoom >/dev/null 2>&1 || break
    sleep 1
  done
fi

SESSION_DIR="$HOME/.zoom/data"
if [[ -d "$SESSION_DIR" ]]; then
  rm -rf "$SESSION_DIR"
  log "Removed $SESSION_DIR - Zoom will show its sign-in screen next launch"
else
  log "No session data found at $SESSION_DIR (already signed out, or this client version stores it elsewhere)"
fi
