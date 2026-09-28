#!/usr/bin/env bash
# Rewrites the x11vnc password file from a new password read on stdin
# (never argv, so it never lands in the process list or sudo logs).
# Called only by the dashboard via its narrow sudoers rule; restarting
# x11vnc.service is left to the caller.
set -euo pipefail

PASSWD_FILE="$HOME/.vnc/passwd"
mkdir -p "$HOME/.vnc"
chmod 700 "$HOME/.vnc"

NEW_PASSWORD="$(cat)"
if [[ -z "$NEW_PASSWORD" ]]; then
  echo "empty password refused" >&2
  exit 1
fi

x11vnc -storepasswd "$NEW_PASSWORD" "$PASSWD_FILE" >/dev/null
chmod 600 "$PASSWD_FILE"
echo "VNC password updated"
