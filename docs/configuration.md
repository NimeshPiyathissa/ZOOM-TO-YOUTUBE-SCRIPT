# Configuration reference

Two places hold configuration, split by sensitivity (see [encryption.md](encryption.md)
for why):

## `.env` (non-secret) — `/home/zoombot/zoom-stream/.env`

| Key | Meaning | Default |
|---|---|---|
| `BOT_NAME` | Display name the bot joins Zoom meetings as | `Stream Bot` |
| `ZOOM_SIGNIN_MODE` | `guest` or `google` | `guest` |
| `RESOLUTION` | Capture/encode resolution | `1920x1080` |
| `FPS` | Frame rate | `30` |
| `VIDEO_BITRATE` | kbps | `6000` |
| `AUDIO_BITRATE` | kbps | `192` |
| `X264_PRESET` | `ultrafast`…`medium` — faster = less CPU, lower quality per bitrate | `veryfast` |
| `DISPLAY_NUM` | X11 display number | `:99` |

Edited from the dashboard's Configuration page; each key writes atomically via
`scripts/write-env.sh` and only restarts the units that actually depend on it (see
`app/config.py`'s `ENV_KEY_RESTART_MAP`).

## The vault (secret) — see [encryption.md](encryption.md)

`ZOOM_LINK`/`ZOOM_PASSCODE`, `YT_STREAM_KEY`, `VNC_PASSWORD`, Telegram credentials.
Never in a plain file once setup has run. Managed via the dashboard UI or
`python -m app.cli vault ...` / `migrate-secrets`.

## `current-source.env` (non-secret, which source is active)

Not hand-edited — written by the dashboard whenever you switch/save a source
(`scripts/write-source.sh`). Holds the active source's type-specific options
(webpage URL, direct-media mode, Zoom join settings) and, since Part 4, the
watermark filter's own config (`WATERMARK_*` keys) — independent of which source
is active, carried forward across a source switch rather than reset.

## Sources

Configuration → Sources: `zoom` (link/passcode/bot name/sign-in mode), `webpage`
(any http/https page, Chrome kiosk), or `direct` (an HLS/RTMP(S)/SRT/file URL
FFmpeg reads itself, optionally stream-copied with no re-encode when `ffprobe`
confirms YouTube-compatible codecs). See the top-level README's "Sources" section
for the full behavior and the URL-security rules every webpage/direct URL passes
through before ever reaching FFmpeg or Chromium.

## Watermark (Part 4)

Overlay Studio page → "Encoder Watermark" section. Text or image, a 9-point
position grid, pixel margins, size, and opacity — burned into the actual FFmpeg
filter graph (`scripts/lib.sh`'s `build_watermark_filter()`), not a preview-only
DOM overlay. Changing it requires restarting the encoder to take effect; the UI
tells you when saved settings don't match what's currently live.

## Minimum spec

4 vCPU / 8GB RAM, no GPU, sized for 1080p30. `libx264 veryfast` at 1080p30/6000kbps
typically uses ~2.5–3.5 of 4 vCPUs on a dedicated-core VPS. If `monitor.sh` shows
`speed=` sustained below `1.0x`, drop to `RESOLUTION=1280x720` / `VIDEO_BITRATE=4000`
before trying anything else — see the README's Performance section.
