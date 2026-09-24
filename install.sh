#!/usr/bin/env bash
# One-command installer for the Zoom/browser -> YouTube streaming bot,
# covering both the streaming pipeline and the web dashboard.
# Target: Ubuntu 22.04 LTS (jammy) x86_64, run as root from the repo root
# you cloned/copied onto the VPS.
#
#   sudo ./install.sh            fresh install (or safe to re-run)
#   sudo ./install.sh upgrade    re-deploy code/deps on an existing install
#   sudo ./install.sh uninstall  stop everything and remove the app
#                                 (add --purge-data to also delete the
#                                 encrypted vault, database, and recordings -
#                                 never implied, always explicit)
#
# What a fresh install does:
#   1. Preflight checks (root, OS, disk space, architecture)
#   2. System packages (Xvfb, Openbox, PipeWire, x11vnc, FFmpeg, Chrome,
#      Python, helpers)
#   3. A dedicated, lingering service user "zoombot" for the pipeline
#   4. The official Zoom Linux client and Google Chrome
#   5. Pipeline scripts + systemd --user units, deployed under zoombot
#   6. Firewall: SSH + 443/tcp (dashboard is directly internet-facing by
#      design, protected by TLS + account lockout, not by network
#      obscurity - see docs/security.md; VNC itself stays loopback-only)
#   7. Hands off to dashboard/install-dashboard.sh for the dashboard,
#      the encrypted secret vault, and the final setup prompts (Part 3)
#
# Every step checks current state first - re-running is always safe.
# Nothing here ever creates a default or fallback password.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_USER="zoombot"
SVC_HOME="/home/${SVC_USER}"
APP_DIR="${SVC_HOME}/zoom-stream"
ACTION="${1:-install}"

# --------------------------------------------------------------- preflight

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root: sudo ./install.sh [install|upgrade|uninstall]" >&2
  exit 1
fi

if [[ "${ACTION}" != "install" && "${ACTION}" != "upgrade" && "${ACTION}" != "uninstall" ]]; then
  echo "Unknown action '${ACTION}'. Usage: ./install.sh [install|upgrade|uninstall] [--purge-data]" >&2
  exit 1
fi

if [[ "${ACTION}" == "install" ]]; then
  echo "==> Preflight checks"

  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "22.04" ]]; then
      echo "    WARNING: this installer targets Ubuntu 22.04 LTS; detected ${PRETTY_NAME:-unknown}." >&2
      echo "    Continuing anyway, but expect rough edges on anything else." >&2
    else
      echo "    OS: ${PRETTY_NAME} - OK"
    fi
  fi

  ARCH="$(uname -m)"
  if [[ "${ARCH}" != "x86_64" ]]; then
    echo "    ERROR: this installer only supports x86_64 (Zoom/Chrome .deb packages require it), detected ${ARCH}." >&2
    exit 1
  fi
  echo "    Architecture: ${ARCH} - OK"

  AVAIL_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
  MIN_KB=$((10 * 1024 * 1024))  # 10 GB
  if [[ "${AVAIL_KB}" -lt "${MIN_KB}" ]]; then
    echo "    ERROR: less than 10GB free on / (found $((AVAIL_KB / 1024 / 1024))GB)." >&2
    echo "    Free up space or resize the disk before installing." >&2
    exit 1
  fi
  echo "    Disk space: $((AVAIL_KB / 1024 / 1024))GB free - OK"

  NPROC="$(nproc)"
  MEM_KB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [[ "${NPROC}" -lt 4 || "${MEM_KB}" -lt $((7 * 1024 * 1024)) ]]; then
    echo "    WARNING: this project is sized for 4 vCPU / 8GB at 1080p30 (found ${NPROC} vCPU / $((MEM_KB / 1024 / 1024))GB)." >&2
    echo "    It may still work at 720p or with fewer/smaller sources - see README's Performance section." >&2
  else
    echo "    Resources: ${NPROC} vCPU / $((MEM_KB / 1024 / 1024))GB - OK"
  fi
fi

# --------------------------------------------------------------- uninstall

if [[ "${ACTION}" == "uninstall" ]]; then
  PURGE_DATA=0
  [[ "${2:-}" == "--purge-data" ]] && PURGE_DATA=1

  echo "==> Uninstalling"
  echo "    This stops and disables every pipeline and dashboard service."
  if [[ "${PURGE_DATA}" -eq 1 ]]; then
    echo "    --purge-data: the encrypted vault, database, and recordings will also be deleted."
  else
    echo "    Data (vault, database, recordings) is kept - re-run with --purge-data to also remove it."
  fi

  if id "${SVC_USER}" &>/dev/null; then
    SVC_UID="$(id -u "${SVC_USER}")"
    sudo -u "${SVC_USER}" XDG_RUNTIME_DIR="/run/user/${SVC_UID}" systemctl --user stop \
      ffmpeg-stream x11vnc zoom browser-source audio-setup openbox xvfb novnc-proxy 2>/dev/null || true
    sudo -u "${SVC_USER}" XDG_RUNTIME_DIR="/run/user/${SVC_UID}" systemctl --user disable \
      ffmpeg-stream x11vnc zoom browser-source audio-setup openbox xvfb novnc-proxy 2>/dev/null || true
  fi
  systemctl stop dashboard.service 2>/dev/null || true
  systemctl disable dashboard.service 2>/dev/null || true
  rm -f /etc/systemd/system/dashboard.service
  rm -f /etc/sudoers.d/dashboard
  systemctl daemon-reload

  if [[ "${PURGE_DATA}" -eq 1 ]]; then
    rm -rf /home/dashboard/data /home/dashboard/certs /etc/zoom-stream
    rm -rf "${APP_DIR}"
    echo "    Data purged."
  else
    echo "    App code removed from ${APP_DIR}/scripts, /home/dashboard/app - data left in place under"
    echo "    /home/dashboard/data and ${SVC_HOME} for a future reinstall."
  fi

  echo ""
  echo "Uninstall complete. The 'zoombot' and 'dashboard' service users were left in place"
  echo "(they own the data above) - remove them by hand with 'deluser' if you're certain."
  exit 0
fi

# ------------------------------------------------------------ install/upgrade

STEP=1
TOTAL_STEPS=7
next_step() { echo "==> [${STEP}/${TOTAL_STEPS}] $1"; STEP=$((STEP + 1)); }

next_step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  xvfb openbox x11vnc wmctrl xdotool x11-xserver-utils x11-utils unclutter \
  at-spi2-core \
  ffmpeg fontconfig \
  pipewire pipewire-pulse wireplumber pipewire-audio-client-libraries pulseaudio-utils \
  dbus-user-session \
  fonts-liberation libasound2 libnss3 libxss1 libgtk-3-0 \
  python3 python3-venv python3-pip websockify openssl \
  wget curl ca-certificates ufw jq

if ! dpkg -s google-chrome-stable &>/dev/null; then
  TMP_DEB="$(mktemp --suffix=.deb)"
  wget -q -O "${TMP_DEB}" "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
  apt-get install -y "${TMP_DEB}"
  rm -f "${TMP_DEB}"
else
  echo "    google-chrome-stable already installed, skipping"
fi

next_step "Creating service user '${SVC_USER}'"
if ! id "${SVC_USER}" &>/dev/null; then
  adduser --system --group --home "${SVC_HOME}" --shell /bin/bash "${SVC_USER}"
fi
mkdir -p "${SVC_HOME}"
loginctl enable-linger "${SVC_USER}"

next_step "Installing Zoom Linux client"
if ! dpkg -s zoom &>/dev/null; then
  TMP_DEB="$(mktemp --suffix=.deb)"
  wget -q -O "${TMP_DEB}" "https://zoom.us/client/latest/zoom_amd64.deb"
  apt-get install -y "${TMP_DEB}"
  rm -f "${TMP_DEB}"
else
  echo "    zoom already installed, skipping"
fi

next_step "Deploying pipeline files to ${APP_DIR}"
mkdir -p "${APP_DIR}/scripts" "${APP_DIR}/logs"
cp -r "${SRC_DIR}/scripts/." "${APP_DIR}/scripts/"
cp -r "${SRC_DIR}/extensions" "${APP_DIR}/" 2>/dev/null || true
chmod +x "${APP_DIR}"/scripts/*.sh
chown -R "${SVC_USER}:${SVC_USER}" "${SVC_HOME}"
if [[ "${ACTION}" == "install" && ! -f "${APP_DIR}/.env" ]]; then
  # Non-secret defaults only - ZOOM_LINK/PASSCODE/YT_STREAM_KEY/VNC_PASSWORD
  # live in the encrypted vault (Part 0), set via install-dashboard.sh's
  # setup step below, never here.
  cat > "${APP_DIR}/.env" <<'EOF'
BOT_NAME=Stream Bot
ZOOM_SIGNIN_MODE=guest
RESOLUTION=1920x1080
FPS=30
VIDEO_BITRATE=6000
AUDIO_BITRATE=192
X264_PRESET=veryfast
DISPLAY_NUM=:99
EOF
  chown "${SVC_USER}:${SVC_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
fi

next_step "Installing systemd --user units"
mkdir -p "${SVC_HOME}/.config/systemd/user"
for unit_file in "${SRC_DIR}"/systemd/*.service; do
  [[ "$(basename "${unit_file}")" == "dashboard.service" ]] && continue
  cp "${unit_file}" "${SVC_HOME}/.config/systemd/user/"
done
chown -R "${SVC_USER}:${SVC_USER}" "${SVC_HOME}/.config"

SVC_UID="$(id -u "${SVC_USER}")"
export XDG_RUNTIME_DIR="/run/user/${SVC_UID}"
systemctl start "user@${SVC_UID}.service" || true
sleep 2

run_user_systemctl() {
  sudo -u "${SVC_USER}" XDG_RUNTIME_DIR="/run/user/${SVC_UID}" systemctl --user "$@"
}
run_user_systemctl daemon-reload
for unit_file in "${SRC_DIR}"/systemd/*.service; do
  name="$(basename "${unit_file}")"
  [[ "${name}" == "dashboard.service" ]] && continue
  run_user_systemctl enable "${name}"
done
echo "    Pipeline units installed and enabled (not started yet)."

next_step "Configuring the firewall"
ufw allow OpenSSH >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable
echo "    ufw: SSH and 443/tcp are open. The dashboard is reachable directly at https://<vps-ip>"
echo "    (no SSH tunnel needed) - VNC itself still stays loopback-only, reached only through"
echo "    the dashboard's own authenticated proxy. If this is a cloud VPS (AWS, etc.), you"
echo "    likely also need to open 443/tcp in its Security Group / cloud firewall console -"
echo "    ufw alone only controls the OS-level firewall, not your cloud provider's."

next_step "Dashboard, encrypted vault, and setup"
"${SRC_DIR}/dashboard/install-dashboard.sh" "${ACTION}"

echo ""
echo "Install complete. See the summary above for the dashboard URL and next steps."
echo "The pipeline itself isn't started yet - connect over the dashboard's Remote GUI"
echo "first (README's 'First-run: connect and verify' section) before ever clicking Go Live."
