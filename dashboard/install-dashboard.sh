#!/usr/bin/env bash
# Idempotent installer for the web dashboard, the encrypted secret vault
# (Part 0), and first-run setup (Part 3). Run as root - normally invoked
# automatically by ../install.sh, not by hand.
#
# Requires the streaming stack (install.sh, one level up) to already be
# installed - this script only adds the dashboard on top of it.
#
#   ./install-dashboard.sh            fresh install (or safe to re-run)
#   ./install-dashboard.sh upgrade    re-deploy code/deps, skip vault setup
#   ./install-dashboard.sh uninstall  handled entirely by install.sh - not
#                                      called directly for this action
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASH_USER="dashboard"
DASH_HOME="/home/${DASH_USER}"
APP_DIR="${DASH_HOME}/app"
DATA_DIR="${DASH_HOME}/data"
CERT_DIR="${DASH_HOME}/certs"
ZOOMBOT_HOME="/home/zoombot"
STREAM_APP_DIR="${ZOOMBOT_HOME}/zoom-stream"
ACTION="${1:-install}"

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root: sudo ./install-dashboard.sh" >&2
  exit 1
fi

if ! id zoombot &>/dev/null; then
  echo "zoombot user not found. Run install.sh (one directory up) first." >&2
  exit 1
fi
if [[ ! -x "${STREAM_APP_DIR}/scripts/write-env.sh" ]]; then
  echo "${STREAM_APP_DIR}/scripts/write-env.sh missing or not executable." >&2
  echo "Re-sync the project and re-run install.sh first (it picks up new scripts/units automatically)." >&2
  exit 1
fi

echo "==> [1/8] Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip websockify openssl

echo "==> [2/8] Creating service user '${DASH_USER}'"
if ! id "${DASH_USER}" &>/dev/null; then
  adduser --system --group --home "${DASH_HOME}" --shell /bin/bash "${DASH_USER}"
fi
mkdir -p "${DASH_HOME}"
usermod -aG systemd-journal "${DASH_USER}"
usermod -aG zoombot "${DASH_USER}"

echo "==> [3/8] Deploying app files to ${APP_DIR}"
mkdir -p "${APP_DIR}" "${DATA_DIR}" "${CERT_DIR}"
cp -r "${SRC_DIR}/app" "${APP_DIR}/"
cp -r "${SRC_DIR}/static" "${APP_DIR}/"
cp -r "${SRC_DIR}/templates" "${APP_DIR}/"
cp "${SRC_DIR}/requirements.txt" "${APP_DIR}/"
chown -R "${DASH_USER}:${DASH_USER}" "${DASH_HOME}"
chmod 700 "${DATA_DIR}" "${CERT_DIR}"

echo "==> [4/8] Python virtualenv + dependencies"
if [[ ! -d "${DASH_HOME}/venv" ]]; then
  sudo -u "${DASH_USER}" python3 -m venv "${DASH_HOME}/venv"
fi
sudo -u "${DASH_USER}" "${DASH_HOME}/venv/bin/pip" install -q --upgrade pip
sudo -u "${DASH_USER}" "${DASH_HOME}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

echo "==> [5/8] TLS certificate (self-signed, for the SSH-tunnel-only access path)"
if [[ ! -f "${CERT_DIR}/cert.pem" ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${CERT_DIR}/key.pem" -out "${CERT_DIR}/cert.pem" \
    -days 3650 -subj "/CN=127.0.0.1" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
  chown "${DASH_USER}:${DASH_USER}" "${CERT_DIR}"/*.pem
  chmod 600 "${CERT_DIR}/key.pem"
  echo "    generated. Your browser will warn about it being self-signed the first time - that's expected for a loopback-only cert."
else
  echo "    already present, skipping"
fi

echo "==> [6/8] Narrow sudoers rule (dashboard -> zoombot, specific commands only)"
# sudoers treats an unescaped comma inside a command argument as a list
# separator, so it must be backslash-escaped here. This is purely a
# sudoers-syntax concern - the actual argv systemctl receives (built in
# app/control.py) is the plain, unescaped comma-separated value, and sudo
# matches it correctly against this escaped pattern.
SHOW_PROPS="ActiveState\,SubState\,Result\,NRestarts\,ActiveEnterTimestamp\,ExecMainStartTimestamp\,ExecMainStatus\,MainPID\,ExecMainCode"
UNITS="xvfb openbox audio-setup zoom browser-source x11vnc novnc-proxy ffmpeg-stream"
SUDOERS_TMP="$(mktemp)"
{
  echo "# Managed by install-dashboard.sh - re-run the installer to regenerate, do not hand-edit."
  echo "Defaults:${DASH_USER} env_keep += \"XDG_RUNTIME_DIR\""
  echo ""
  for unit in ${UNITS}; do
    for verb in start stop restart reset-failed; do
      echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/systemctl --user ${verb} ${unit}.service"
    done
    echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/systemctl --user show ${unit}.service --property=${SHOW_PROPS}"
  done
  echo ""
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/cat ${STREAM_APP_DIR}/.env"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/cat ${STREAM_APP_DIR}/current-source.env"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/write-env.sh"
  # Part 0's tmpfs runtime bridge (app/runtime_env.py) - regenerates the
  # merged non-secret+vault-secret env file the pipeline scripts actually
  # source, on tmpfs instead of persistent disk. Not yet called from any
  # live code path - see config.py's WRITE_RUNTIME_ENV_SCRIPT docstring.
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/write-runtime-env.sh"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/write-source.sh"
  # Part 4's watermark image uploads (app/control.py's write_watermark_image())
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/write-watermark-image.sh *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/rotate-vnc-password.sh"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/test-recording.sh"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/zoom-google-signin.sh"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/zoom-signout.sh"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/test -e *"
  echo ""
  echo "${DASH_USER} ALL=(root) NOPASSWD: /usr/sbin/reboot"
  echo ""
  echo "# Touch remote / media control"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/set-stream-audio.sh *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/xsetroot -solid *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/chrome-account.sh *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/zoom-shortcut.sh *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/python3 ${STREAM_APP_DIR}/scripts/zoom-atspi.py *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/python3 ${STREAM_APP_DIR}/scripts/zoom-status.py"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/python3 ${STREAM_APP_DIR}/scripts/audio-level.py *"
  echo ""
  echo "# Interactive preview support, Zoom dialog handling, audio proof"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/python3 ${STREAM_APP_DIR}/scripts/zoom-dialog.py list"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: /usr/bin/python3 ${STREAM_APP_DIR}/scripts/zoom-dialog.py dismiss"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/focus-window.sh zoom"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/focus-window.sh browser"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/set-vnc-rate.sh fast"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/set-vnc-rate.sh slow"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/audio-selftest.sh"
  echo ""
  echo "# /remote 'Browser' - Google in the signed-in Chrome profile; local recording"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/open-browser.sh *"
  echo "${DASH_USER} ALL=(zoombot) NOPASSWD: ${STREAM_APP_DIR}/scripts/record-stream.sh *"
} > "${SUDOERS_TMP}"

if ! visudo -cf "${SUDOERS_TMP}"; then
  echo "Generated sudoers file failed validation, aborting without installing it." >&2
  rm -f "${SUDOERS_TMP}"
  exit 1
fi
install -o root -g root -m 440 "${SUDOERS_TMP}" /etc/sudoers.d/dashboard
rm -f "${SUDOERS_TMP}"

echo "==> [7/8] Installing systemd units"
cp "${SRC_DIR}/systemd/dashboard.service" /etc/systemd/system/dashboard.service
systemctl daemon-reload
systemctl enable dashboard.service

ZUID="$(id -u zoombot)"
systemctl start "user@${ZUID}.service" || true
sleep 1
sudo -u zoombot XDG_RUNTIME_DIR="/run/user/${ZUID}" systemctl --user daemon-reload
sudo -u zoombot XDG_RUNTIME_DIR="/run/user/${ZUID}" systemctl --user enable novnc-proxy.service

echo "==> [8/8] Starting the dashboard"
systemctl restart dashboard.service
sleep 1
systemctl --no-pager --lines=5 status dashboard.service || true

# ------------------------------------------------------ Part 3: setup flow

VPS_IP="$(curl -s --max-time 3 https://ifconfig.me || true)"

if [[ "${ACTION}" == "upgrade" ]]; then
  echo ""
  echo "Upgrade complete. The dashboard was restarted; the pipeline (Xvfb/Zoom/"
  echo "FFmpeg) was left untouched - restart it yourself if this upgrade changed"
  echo "anything it depends on."
  exit 0
fi

VAULT_ALREADY_SET_UP=0
if sudo -u "${DASH_USER}" "${DASH_HOME}/venv/bin/python" -c "
import sys; sys.path.insert(0, '${APP_DIR}')
from app import secret_store
sys.exit(0 if secret_store.is_initialized() else 1)
" 2>/dev/null; then
  VAULT_ALREADY_SET_UP=1
fi

if [[ "${VAULT_ALREADY_SET_UP}" -eq 1 ]]; then
  echo ""
  echo "Encrypted secret store already exists - skipping setup."
  echo "Use 'vault change-master-password' or 'vault reset' (see docs) instead."
else
  echo ""
  echo "==> Setup: Master Encryption Password, dashboard admin login, VNC password"
  echo "    (Set ZOOMBOT_MASTER_PASSWORD/ZOOMBOT_ADMIN_USERNAME/ZOOMBOT_ADMIN_PASSWORD/"
  echo "    ZOOMBOT_VNC_PASSWORD/ZOOMBOT_UNLOCK_MODE beforehand to run this non-interactively.)"
  cd "${APP_DIR}"
  # Run as root (this script already is), not `sudo -u dashboard` - the
  # default "cached" unlock mode writes a root-owned key cache file
  # (config.MASTER_KEY_CACHE_FILE) and refuses to proceed otherwise (see
  # cli.py's _is_root() check). secret_store.py's own chown logic already
  # fixes ownership of the files *it* writes; the blanket chown below
  # after setup runs catches everything else this step creates as root
  # (dashboard.db via db.init_db(), any other file under DATA_DIR).
  "${DASH_HOME}/venv/bin/python" -m app.cli setup
  chown -R "${DASH_USER}:${DASH_USER}" "${DATA_DIR}"
fi

echo ""
echo "============================================================"
echo " Install complete"
echo "============================================================"
echo " Server IP        : ${VPS_IP:-<check your cloud provider dashboard>}"
echo " Dashboard URL     : https://127.0.0.1:8443 (via the SSH tunnel below)"
echo " Config location   : ${STREAM_APP_DIR}/.env (non-secret), ${DATA_DIR}/secrets.enc.json (vault)"
echo " Log location       : ${STREAM_APP_DIR}/logs/"
echo " Open ports (public): SSH only (ufw) - dashboard and VNC are loopback-only"
echo ""
echo " Connect from your own machine:"
echo "   ssh -i \"<path to .pem>\" -N -L 8443:127.0.0.1:8443 ubuntu@${VPS_IP:-<VPS_IP>}"
echo "   open https://127.0.0.1:8443  (accept the self-signed certificate warning once)"
echo ""
echo " Next steps: sign in to the dashboard, set a YouTube stream key, sign a Google"
echo " account in via noVNC if needed, add a meeting, and read README.md's"
echo " 'First-run: connect and verify' section before ever going live."
echo ""
echo " This summary may contain secrets if you set them interactively just now -"
echo " do not paste it anywhere, including into chat with an AI assistant."
echo "============================================================"
