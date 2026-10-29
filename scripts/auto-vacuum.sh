#!/usr/bin/env bash
# ==============================================================================
# Daily disk maintenance for the zoombot user (zoom-vacuum.timer, a --user unit)
# - Prunes Chrome caches, but only in Chrome profiles that are NOT in use right
#   now: deleting Cache/GPUCache under a running Chrome (the stream kiosk) can
#   break its rendering mid-stream, and cookies/sign-ins are never touched.
# - Removes this user's stale ffmpeg temp chunks.
# The systemd journal is NOT vacuumed here - that needs root, which a --user
# unit can't get (the old User=root line made every run fail with
# 216/GROUP). Journal retention is journald's own configuration, managed
# separately from this unit.
# ==============================================================================

set -uo pipefail
cd "${HOME}" || exit 1  # find(1) fails outright from an unreadable cwd

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "Starting automated disk maintenance..."

# 1. Chrome caches. A Chrome user-data-dir is any directory holding a
#    "Local State" file; Chrome keeps a SingletonLock symlink in it for as
#    long as it's running.
CONFIG_DIR="${HOME}/.config"
if [[ -d "$CONFIG_DIR" ]]; then
  while IFS= read -r -d '' state_file; do
    profile_dir="$(dirname "$state_file")"
    if [[ -L "$profile_dir/SingletonLock" || -e "$profile_dir/SingletonLock" ]]; then
      log "Skipping $profile_dir (Chrome is running on it)"
      continue
    fi
    log "Pruning caches in $profile_dir"
    find "$profile_dir" -maxdepth 3 -type d \( -name "Cache" -o -name "Code Cache" -o -name "GPUCache" \) \
      -prune -exec rm -rf {} + 2>/dev/null || true
  done < <(find "$CONFIG_DIR" -maxdepth 3 -name "Local State" -type f -print0 2>/dev/null)
fi

# 2. Leftover ffmpeg temp chunks older than a day - this user's own only.
find /tmp -maxdepth 1 -user "$(id -un)" -name "ffmpeg*.tmp" -mtime +1 -delete 2>/dev/null || true

log "Automated disk maintenance completed successfully."
