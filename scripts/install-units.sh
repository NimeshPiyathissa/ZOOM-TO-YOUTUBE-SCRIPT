#!/usr/bin/env bash
# Installs (or updates) the systemd --user unit files from systemd/ in
# this repo into ~/.config/systemd/user/, then reloads the user manager.
# Idempotent: safe to re-run any time after editing a unit here. This
# never starts, stops, or restarts any unit - `daemon-reload` only makes
# systemd re-read unit definitions; units already running keep running
# unaffected, and only pick up the new Restart=/StartLimit* behavior the
# next time they actually need to restart.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

for unit in "$REPO_DIR"/systemd/*.service; do
  install -m 644 "$unit" "$UNIT_DIR/$(basename "$unit")"
done

systemctl --user daemon-reload
echo "Installed $(ls "$REPO_DIR"/systemd/*.service | wc -l) unit(s) to $UNIT_DIR and reloaded the user manager."
