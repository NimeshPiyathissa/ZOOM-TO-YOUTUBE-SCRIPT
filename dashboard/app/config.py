"""Central configuration for the dashboard. No secrets live here - only
paths, allowlisted names, and non-sensitive defaults."""
from __future__ import annotations

import pathlib

# --- Identities / paths on the VPS ---
ZOOMBOT_USER = "zoombot"
ZOOMBOT_HOME = pathlib.Path("/home/zoombot")
STREAM_APP_DIR = ZOOMBOT_HOME / "zoom-stream"
STREAM_ENV_FILE = STREAM_APP_DIR / ".env"
STREAM_ENV_BACKUP = STREAM_APP_DIR / ".env.bak"
STREAM_SCRIPTS_DIR = STREAM_APP_DIR / "scripts"
STREAM_LOGS_DIR = STREAM_APP_DIR / "logs"
FFMPEG_LOG = STREAM_LOGS_DIR / "ffmpeg.log"
ZOOM_LOG = STREAM_LOGS_DIR / "zoom.log"
BROWSER_LOG = STREAM_LOGS_DIR / "browser.log"
LATEST_TEST_RECORDING = STREAM_LOGS_DIR / "latest-test.mp4"
BROWSER_LOADED_MARKER = STREAM_LOGS_DIR / "browser-loaded"

# Non-secret "which source is currently selected" file. Deliberately
# separate from .env (which holds Zoom/YouTube secrets): URLs and
# playback options for webpage/direct sources aren't secrets, and
# keeping them out of .env means env_store's secret-handling code never
# has to reason about them.
SOURCE_ENV_FILE = STREAM_APP_DIR / "current-source.env"

WRITE_ENV_SCRIPT = STREAM_SCRIPTS_DIR / "write-env.sh"
# Regenerates the pipeline's *runtime* env file - the merge of non-secret
# .env keys with the vault's current secrets - on a tmpfs-backed path
# instead of persistent disk (Part 0's runtime bridge; see
# app/runtime_env.py). Not yet wired into any live systemd unit - see
# docs/encryption.md once Phase B lands it.
WRITE_RUNTIME_ENV_SCRIPT = STREAM_SCRIPTS_DIR / "write-runtime-env.sh"
WRITE_SOURCE_SCRIPT = STREAM_SCRIPTS_DIR / "write-source.sh"
# Part 4: watermark image uploads - see app/control.py's write_watermark_image().
WRITE_WATERMARK_IMAGE_SCRIPT = STREAM_SCRIPTS_DIR / "write-watermark-image.sh"
ROTATE_VNC_SCRIPT = STREAM_SCRIPTS_DIR / "rotate-vnc-password.sh"
TEST_RECORDING_SCRIPT = STREAM_SCRIPTS_DIR / "test-recording.sh"
ZOOM_SIGNIN_SCRIPT = STREAM_SCRIPTS_DIR / "zoom-google-signin.sh"
ZOOM_SIGNOUT_SCRIPT = STREAM_SCRIPTS_DIR / "zoom-signout.sh"
RECORD_STREAM_SCRIPT = STREAM_SCRIPTS_DIR / "record-stream.sh"
AUTO_VACUUM_SCRIPT = STREAM_SCRIPTS_DIR / "auto-vacuum.sh"
DASHBOARD_HOME = pathlib.Path("/home/dashboard")
APP_DIR = DASHBOARD_HOME / "app" if (DASHBOARD_HOME / "app").exists() else pathlib.Path(__file__).resolve().parent.parent
RECORDINGS_DIR = DASHBOARD_HOME / "recordings" if DASHBOARD_HOME.exists() else (APP_DIR / "recordings")
SETTINGS_FILE = APP_DIR / "settings.json"

CHROME_PROFILE_DIR = ZOOMBOT_HOME / ".config" / "stream-chrome-profile"

DATA_DIR = DASHBOARD_HOME / "data" if DASHBOARD_HOME.exists() else (APP_DIR / "data")
DB_PATH = DATA_DIR / "dashboard.db"
OVERLAY_CONFIG_FILE = DATA_DIR / "overlay.json"
BRB_SLATE_FILE = DATA_DIR / "brb_slate.json"
CERT_DIR = DASHBOARD_HOME / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"

# --- Encrypted secret store (Part 0) ---
# The encrypted blob itself is dashboard-owned, same protection level as
# dashboard.db (DATA_DIR is chmod 700, owned by the dashboard user).
SECRET_STORE_FILE = DATA_DIR / "secrets.enc.json"
# A manual `lock` sets this dashboard-owned sentinel so cached-mode
# auto-unlock refuses to silently re-open the vault on the next process
# start until an admin proves they still hold the master password again -
# see secret_store.lock()/try_auto_unlock(). Deliberately NOT the same
# file as the root-owned key cache below, which this process can't write.
VAULT_LOCK_SENTINEL_FILE = DATA_DIR / "vault.locked"

# The cached-mode master key. Root-owned so only setup/change-master-
# password/set-unlock-mode (run via `sudo python -m app.cli ...`) can
# write it; group `dashboard` (the service account is already a member,
# same pattern as its zoombot group membership) so the running dashboard
# process can read it at startup for unattended auto-unlock. See
# docs/encryption.md for the honest limitation this implies (protects
# against a stolen disk/backup or a non-root compromise, not root).
MASTER_KEY_CACHE_DIR = pathlib.Path("/etc/zoom-stream")
MASTER_KEY_CACHE_FILE = MASTER_KEY_CACHE_DIR / "master.key"
DASHBOARD_USER = "dashboard"
DASHBOARD_GROUP = "dashboard"

UNLOCK_MODES = {"cached", "prompt"}
DEFAULT_UNLOCK_MODE = "cached"

# Which settings.json fields are secret (move into the vault) vs. plain
# config (stay in settings.json). Mirrors the ALL_ENV_KEYS/NON_SECRET_ENV_KEYS
# split below for .env. telegram_chat_id/telegram_api_id are identifiers,
# not credentials - Telegram's own UI shows both openly - so they stay
# non-secret; telegram_api_hash and telegram_session_string are as
# sensitive as a login token and must not.
SETTINGS_SECRET_KEYS = {"telegram_bot_token", "telegram_api_hash", "telegram_session_string", "vnc_password"}

DISPLAY_NUM = ":99"

# --- systemd --user units managed by the dashboard, in dependency order ---
# (order matters for "restart whole pipeline"). "zoom" and "browser-source"
# are alternative producers - only one is ever started at a time, selected
# by the active source's type; both are listed here so the dashboard can
# control either, but pipeline_start()/pipeline_stop() only touch the one
# the active source actually needs (see control.start_source).
UNIT_ORDER = ["xvfb", "openbox", "audio-setup", "zoom", "browser-source", "x11vnc", "novnc-proxy", "ffmpeg-stream"]
STOP_ORDER = list(reversed(UNIT_ORDER))
ALLOWED_UNITS = set(UNIT_ORDER)

# Producer units, mutually exclusive - the capture source for zoom/webpage
# source types. Direct-media sources use neither (ffmpeg reads the URL
# itself). A zoom source can ALSO run on browser-source (join_method=web -
# Zoom's own web client in the Chrome kiosk instead of the desktop client),
# so which unit a zoom source actually needs isn't a static lookup by type
# any more - see control.producer_unit_for().

# Units surfaced individually on the Overview page (novnc-proxy is plumbing,
# hidden from the main status grid but still controllable). "zoom" and
# "browser-source" are both listed; the dashboard shows only the one
# relevant to the active source (see api_state's "producer_unit").
VISIBLE_UNITS = ["xvfb", "openbox", "audio-setup", "zoom", "browser-source", "x11vnc", "ffmpeg-stream"]

# --- .env keys and which units restarting them requires ---
# "full-pipeline" means: stop everything, start everything in UNIT_ORDER.
ENV_KEY_RESTART_MAP = {
    "ZOOM_LINK": ["zoom"],
    "ZOOM_PASSCODE": ["zoom"],
    "BOT_NAME": ["zoom"],
    "ZOOM_SIGNIN_MODE": ["zoom"],
    "YT_STREAM_KEY": ["ffmpeg-stream"],
    "FPS": ["ffmpeg-stream"],
    "VIDEO_BITRATE": ["ffmpeg-stream"],
    "AUDIO_BITRATE": ["ffmpeg-stream"],
    "X264_PRESET": ["ffmpeg-stream"],
    "RESOLUTION": ["full-pipeline"],
    "DISPLAY_NUM": ["full-pipeline"],
    "TELEGRAM_BOT_TOKEN": [],
    "TELEGRAM_CHAT_ID": [],
}
# VNC_PASSWORD is handled separately (rotate-vnc-password.sh + restart x11vnc),
# never through the generic .env restart map.

SECRET_ENV_KEYS = {"ZOOM_LINK", "ZOOM_PASSCODE", "YT_STREAM_KEY", "VNC_PASSWORD", "TELEGRAM_BOT_TOKEN"}
NON_SECRET_ENV_KEYS = [
    "BOT_NAME", "RESOLUTION", "FPS", "VIDEO_BITRATE", "AUDIO_BITRATE", "X264_PRESET", "DISPLAY_NUM", "ZOOM_SIGNIN_MODE",
    "TELEGRAM_CHAT_ID",
]
# X264_PRESET must live in ALL_ENV_KEYS: env_store.write_updates rewrites
# .env from exactly these keys, so a key missing here is silently dropped
# on the next config save (that would revert the encoder to stream.sh's
# veryfast default and bring back the CPU overload the ultrafast preset
# fixed).
ALL_ENV_KEYS = [
    "ZOOM_LINK", "ZOOM_PASSCODE", "BOT_NAME", "ZOOM_SIGNIN_MODE", "YT_STREAM_KEY",
    "RESOLUTION", "FPS", "VIDEO_BITRATE", "AUDIO_BITRATE", "X264_PRESET", "VNC_PASSWORD", "DISPLAY_NUM",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]
ZOOM_SIGNIN_MODES = {"guest", "google"}
# Which app actually joins the meeting: the desktop Linux client (existing
# zoommtg:// deep-link path), Zoom's own web client (runs on the
# browser-source producer instead - see control.py's _producer_unit), or
# "auto" (try client, fall back to web only on a mechanism-level failure).
ZOOM_JOIN_MODES = {"client", "web", "auto"}
# x264 presets the panel offers, fastest (least CPU) first. ultrafast is
# the current default on this box for headroom while live at 720p.
X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]

RESOLUTION_PRESETS = {
    "1080p30": {"RESOLUTION": "1920x1080", "FPS": "30", "VIDEO_BITRATE": "6000", "AUDIO_BITRATE": "192"},
    "720p30": {"RESOLUTION": "1280x720", "FPS": "30", "VIDEO_BITRATE": "4000", "AUDIO_BITRATE": "160"},
}
RESOLUTION_RANGE = {"min_w": 640, "max_w": 1920, "min_h": 360, "max_h": 1080}
FPS_RANGE = (15, 30)
VIDEO_BITRATE_RANGE_KBPS = (1000, 8000)
AUDIO_BITRATE_RANGE_KBPS = (96, 320)

# --- server ---
BIND_HOST = "127.0.0.1"
BIND_PORT = 8443
COOKIE_NAME = "zsdash_session"
SESSION_IDLE_TIMEOUT_SECONDS = 12 * 3600
SESSION_ABSOLUTE_TIMEOUT_SECONDS = 7 * 24 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_WINDOW_SECONDS = 15 * 60

def _detect_system_timezone() -> str:
    """Portability fix: this used to be a hardcoded personal timezone.
    Reads the system's actual configured timezone (standard on Debian/
    Ubuntu - `timedatectl set-timezone` writes exactly this file), falling
    back to UTC. No installer prompt needed - the VPS's own timezone,
    which the operator already controls the normal Linux way, is the
    right answer for scheduling join/go-live/stop times."""
    try:
        tz = pathlib.Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if tz:
            return tz
    except OSError:
        pass
    return "UTC"


TIMEZONE = _detect_system_timezone()

# Trust X-Forwarded-Proto for the Secure cookie flag only when running
# behind a local reverse proxy (e.g. optional Caddy setup). False = the
# app terminates TLS itself (self-signed cert) for the SSH-tunnel-only path.
TRUST_FORWARDED_PROTO = False

# --- sources (Change 1) ---
SOURCE_TYPES = {"zoom", "webpage", "direct"}

# Schemes ever allowed in a source URL, full stop - enforced in
# app/url_security.py before any URL reaches ffmpeg/Chromium argv.
ALLOWED_URL_SCHEMES = {"http", "https", "rtmp", "rtmps", "srt"}
# Of those, which are meaningful for each source type.
SCHEMES_BY_TYPE = {
    "webpage": {"http", "https"},
    "direct": {"http", "https", "rtmp", "rtmps", "srt"},
}

DIRECT_MODES = {"copy", "reencode"}
# Codecs ffprobe may report that we consider safe to mux straight through
# to YouTube's RTMP ingest without re-encoding.
YOUTUBE_COMPATIBLE_VIDEO_CODECS = {"h264"}
YOUTUBE_COMPATIBLE_AUDIO_CODECS = {"aac"}

WEBPAGE_ZOOM_RANGE = (0.5, 2.0)
WEBPAGE_RELOAD_RANGE_SECONDS = (0, 24 * 3600)  # 0 = never

PROBE_TIMEOUT_SECONDS = 15
FFPROBE_BIN = "/usr/bin/ffprobe"
GOOGLE_CHROME_BIN = "/usr/bin/google-chrome-stable"
UNCLUTTER_BIN = "/usr/bin/unclutter"

# --- touch remote / media control (Part 3) ---

# Must match scripts/browser-source.sh's --remote-debugging-port in the
# zoom-stream repo - bound to 127.0.0.1 there, so this is only ever
# reached from this same box, same as x11vnc/novnc-proxy.
CHROME_DEBUG_PORT = 9222

# Must match scripts/audio-setup.sh's null sink name in the zoom-stream
# repo (`zoom_out`) - muting this sink silences whatever stream.sh's
# ffmpeg captures from `zoom_out.monitor` without touching ffmpeg itself,
# so the RTMP connection never drops.
STREAM_AUDIO_SINK = "zoom_out"

XDOTOOL_BIN = "/usr/bin/xdotool"
WMCTRL_BIN = "/usr/bin/wmctrl"
PACTL_BIN = "/usr/bin/pactl"
