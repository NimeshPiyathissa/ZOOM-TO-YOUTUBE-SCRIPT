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
WRITE_SOURCE_SCRIPT = STREAM_SCRIPTS_DIR / "write-source.sh"
ROTATE_VNC_SCRIPT = STREAM_SCRIPTS_DIR / "rotate-vnc-password.sh"
TEST_RECORDING_SCRIPT = STREAM_SCRIPTS_DIR / "test-recording.sh"
ZOOM_SIGNIN_SCRIPT = STREAM_SCRIPTS_DIR / "zoom-google-signin.sh"
ZOOM_SIGNOUT_SCRIPT = STREAM_SCRIPTS_DIR / "zoom-signout.sh"

CHROME_PROFILE_DIR = ZOOMBOT_HOME / ".config" / "stream-chrome-profile"

DASHBOARD_HOME = pathlib.Path("/home/dashboard")
DATA_DIR = DASHBOARD_HOME / "data"
DB_PATH = DATA_DIR / "dashboard.db"
CERT_DIR = DASHBOARD_HOME / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"

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
# source types. Direct-media sources use neither (ffmpeg reads the URL itself).
PRODUCER_UNITS = {"zoom": "zoom", "webpage": "browser-source"}

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
}
# VNC_PASSWORD is handled separately (rotate-vnc-password.sh + restart x11vnc),
# never through the generic .env restart map.

SECRET_ENV_KEYS = {"ZOOM_LINK", "ZOOM_PASSCODE", "YT_STREAM_KEY", "VNC_PASSWORD"}
NON_SECRET_ENV_KEYS = [
    "BOT_NAME", "RESOLUTION", "FPS", "VIDEO_BITRATE", "AUDIO_BITRATE", "X264_PRESET", "DISPLAY_NUM", "ZOOM_SIGNIN_MODE",
]
# X264_PRESET must live in ALL_ENV_KEYS: env_store.write_updates rewrites
# .env from exactly these keys, so a key missing here is silently dropped
# on the next config save (that would revert the encoder to stream.sh's
# veryfast default and bring back the CPU overload the ultrafast preset
# fixed).
ALL_ENV_KEYS = [
    "ZOOM_LINK", "ZOOM_PASSCODE", "BOT_NAME", "ZOOM_SIGNIN_MODE", "YT_STREAM_KEY",
    "RESOLUTION", "FPS", "VIDEO_BITRATE", "AUDIO_BITRATE", "X264_PRESET", "VNC_PASSWORD", "DISPLAY_NUM",
]
ZOOM_SIGNIN_MODES = {"guest", "google"}
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

TIMEZONE = "Asia/Colombo"

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
