#!/usr/bin/env bash
# ==============================================================================
# Automated Disk Maintenance & Auto-Vacuum Script
# - Cleans systemd journal logs to max 200MB
# - Prunes Chrome browser caches in user profiles
# - Cleans old temp files
# ==============================================================================

set -uo pipefail

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting automated disk maintenance..."

# 1. Vacuum journalctl logs to 200MB
if command -v journalctl >/dev/null 2>&1; then
  echo "Vacuuming systemd journal to 200M..."
  journalctl --vacuum-size=200M 2>/dev/null || true
fi

# 2. Prune Chrome browser caches (older than 3 days or excess cache)
ZOOMBOT_HOME="/home/zoombot"
if [[ -d "$ZOOMBOT_HOME/.config" ]]; then
  echo "Pruning old Chrome caches under $ZOOMBOT_HOME/.config..."
  find "$ZOOMBOT_HOME/.config" -type d \( -name "Cache" -o -name "Code Cache" -o -name "GPUCache" \) -prune -exec rm -rf {} + 2>/dev/null || true
fi

# 3. Clean temporary recording files or leftover chunks
if [[ -d "/tmp" ]]; then
  find /tmp -name "ffmpeg*.tmp" -mtime +1 -delete 2>/dev/null || true
fi

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Automated disk maintenance completed successfully."
