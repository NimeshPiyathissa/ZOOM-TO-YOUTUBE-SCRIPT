#!/usr/bin/env bash
# fix-fonts-and-browser.sh
#
# Idempotent setup for:
#   1. System fonts (Latin/Sinhala/Tamil/CJK/emoji coverage) for Zoom's
#      native Qt UI and any system-font-dependent rendering on :99.
#   2. Zoom Qt scale-factor pinning (explicit scale 1, no auto-DPI guess).
#   3. Chrome kiosk launch flags for the "webpage" stream source
#      (browser-source.sh), tuned for software rendering under Xvfb.
#
# Safe to re-run: every step checks current state before changing
# anything. Run as root (sudo bash fix-fonts-and-browser.sh) on a fresh
# Ubuntu 22.04 zoom-stream install, after zoombot's zoom-stream directory
# already exists.
set -euo pipefail

ZOOMBOT_HOME=/home/zoombot
APP_DIR="$ZOOMBOT_HOME/zoom-stream"
UNIT_DIR="$ZOOMBOT_HOME/.config/systemd/user"
ZOOMBOT_UID="$(id -u zoombot)"

log() { echo "[fix-fonts-and-browser] $*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash $0)" >&2
  exit 1
fi

as_zoombot() {
  sudo -u zoombot XDG_RUNTIME_DIR="/run/user/${ZOOMBOT_UID}" "$@"
}

# ---------------------------------------------------------------------
# 1. Fonts
# ---------------------------------------------------------------------
log "Checking disk space before installing fonts..."
AVAIL_KB="$(df --output=avail / | tail -1)"
AVAIL_MB=$((AVAIL_KB / 1024))
log "Free space: ${AVAIL_MB}MB"
if (( AVAIL_MB < 3072 + 250 )); then
  echo "WARNING: free space (${AVAIL_MB}MB) is close to the 3GB floor for a ~250MB install. Aborting font install." >&2
  exit 1
fi

FONT_PKGS=(fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fonts-liberation fonts-liberation2 fonts-dejavu fonts-lklug-sinhala)
MISSING_PKGS=()
for p in "${FONT_PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING_PKGS+=("$p")
done
if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  log "Installing missing font packages: ${MISSING_PKGS[*]}"
  apt-get update -qq
  apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}"
else
  log "All core font packages already installed, skipping."
fi

# ttf-mscorefonts-installer: EULA preseed + non-interactive install with
# a timeout; fall back to fonts-freefont-ttf if the sourceforge download
# fails (common on restricted networks).
if ! dpkg -s ttf-mscorefonts-installer >/dev/null 2>&1; then
  log "Preseeding ttf-mscorefonts-installer EULA..."
  echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections
  log "Installing ttf-mscorefonts-installer (timeout 120s)..."
  export DEBIAN_FRONTEND=noninteractive
  if timeout 120 apt-get install -y ttf-mscorefonts-installer; then
    log "ttf-mscorefonts-installer installed."
  else
    log "WARNING: ttf-mscorefonts-installer failed or timed out (likely blocked sourceforge download). Falling back to fonts-freefont-ttf."
    apt-get install -y --no-install-recommends fonts-freefont-ttf
  fi
else
  log "ttf-mscorefonts-installer already installed, skipping."
fi

# ---------------------------------------------------------------------
# 2. Fontconfig defaults
# ---------------------------------------------------------------------
LOCAL_CONF=/etc/fonts/local.conf
LOCAL_CONF_SRC="$(dirname "${BASH_SOURCE[0]}")/local.conf"
if [[ -f "$LOCAL_CONF_SRC" ]]; then
  if ! cmp -s "$LOCAL_CONF_SRC" "$LOCAL_CONF" 2>/dev/null; then
    log "Installing $LOCAL_CONF"
    install -m 644 "$LOCAL_CONF_SRC" "$LOCAL_CONF"
  else
    log "$LOCAL_CONF already up to date."
  fi
else
  echo "ERROR: local.conf not found next to this script" >&2
  exit 1
fi

log "Rebuilding font caches (system + zoombot)..."
fc-cache -f >/tmp/fc-cache-system.log 2>&1
as_zoombot fc-cache -f >/tmp/fc-cache-zoombot.log 2>&1

# ---------------------------------------------------------------------
# 3. Zoom: backup config, pin Qt scale factor to 1
# ---------------------------------------------------------------------
BACKUP_DIR="$ZOOMBOT_HOME/config-backups/$(date +%Y%m%d)"
if [[ ! -d "$BACKUP_DIR" ]]; then
  log "Backing up Zoom config to $BACKUP_DIR"
  mkdir -p "$BACKUP_DIR"
  [[ -d "$ZOOMBOT_HOME/.zoom" ]] && cp -a "$ZOOMBOT_HOME/.zoom" "$BACKUP_DIR/.zoom"
  [[ -f "$ZOOMBOT_HOME/.config/zoomus.conf" ]] && cp -a "$ZOOMBOT_HOME/.config/zoomus.conf" "$BACKUP_DIR/zoomus.conf"
  chown -R zoombot:zoombot "$BACKUP_DIR"
else
  log "Zoom config backup for today already exists ($BACKUP_DIR), skipping."
fi

ZOOM_UNIT="$UNIT_DIR/zoom.service"
if [[ -f "$ZOOM_UNIT" ]] && ! grep -q "QT_SCALE_FACTOR" "$ZOOM_UNIT"; then
  log "Adding QT scale-factor env vars to zoom.service"
  sed -i '/^Environment=DISPLAY=:99/a Environment=QT_SCALE_FACTOR=1\nEnvironment=QT_AUTO_SCREEN_SCALE_FACTOR=0' "$ZOOM_UNIT"
else
  log "zoom.service already has QT scale-factor env vars (or unit missing), skipping."
fi

# ---------------------------------------------------------------------
# 4. Chrome launch flags (browser-source.sh)
# ---------------------------------------------------------------------
BROWSER_SCRIPT="$APP_DIR/scripts/browser-source.sh"
if [[ -f "$BROWSER_SCRIPT" ]]; then
  if ! grep -q -- '--disable-gpu' "$BROWSER_SCRIPT"; then
    log "Adding software-rendering + locale/password-store flags to browser-source.sh"
    sed -i "/--overscroll-history-navigation=0 \\\\/a\\  --disable-gpu --lang=en-US --password-store=basic \\\\" "$BROWSER_SCRIPT"
  else
    log "browser-source.sh already has --disable-gpu, skipping flag insert."
  fi
  if grep -q -- '--disable-features=TranslateUI,Notifications' "$BROWSER_SCRIPT"; then
    log "Widening --disable-features to include Translate"
    sed -i 's/--disable-features=TranslateUI,Notifications/--disable-features=Translate,TranslateUI,Notifications/' "$BROWSER_SCRIPT"
  fi
else
  echo "WARNING: $BROWSER_SCRIPT not found, skipping Chrome flag update." >&2
fi

# ---------------------------------------------------------------------
# 5. Reload zoombot's systemd user units
# ---------------------------------------------------------------------
log "Reloading zoombot systemd --user daemon..."
as_zoombot systemctl --user daemon-reload

log "Done. Verify with: fc-match, and restarting the display chain (see README)."
