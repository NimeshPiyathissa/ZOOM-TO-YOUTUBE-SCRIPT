# Troubleshooting

## No audio in the stream

Check `pactl list short sinks` (as `zoombot`, with `XDG_RUNTIME_DIR` set) shows
`zoom_out`, and that it's the default sink (`pactl get-default-sink`). In Zoom's
audio settings, speaker output must be `zoom_out`, not a real device. Restart
`audio-setup.service` then `zoom.service` if it drifted.

## Black screen / frozen video

Confirm Xvfb and Openbox are both active (`manage.sh status`). Reconnect over VNC —
if the window isn't visible or maximized, `wmctrl -l` (as `zoombot`, `DISPLAY=:99`)
lists windows; maximize by hand once, it's remembered after that.

## Zoom asks for login or a CAPTCHA on join

Deep-link joins can occasionally trigger this for new IPs/first runs. Connect over
VNC, complete it manually once. Consider allow-listing the VPS IP in your Zoom
account's meeting settings if it recurs.

## YouTube shows "Poor" stream health

Almost always CPU (encoder falling behind — check `monitor.sh`'s `speed=`) or
upload bandwidth. Drop to 720p per the configuration doc's Performance section, or
lower `VIDEO_BITRATE`. Confirm outbound bandwidth with `iperf3`/`speedtest-cli` if
it's not the CPU.

## Audio/video out of sync

Usually audio drift from PipeWire buffering, not FFmpeg. Confirm
`aresample=async=1` is present in the running command (`ps aux | grep ffmpeg`, or
check `logs/ffmpeg.log`) and both inputs use `-use_wallclock_as_timestamps 1` (they
do by default). If it persists after a fresh restart of the whole stack, it's more
often the null sink carrying stale buffered audio from a previous session —
`manage.sh down` then `up` clears it.

## Watermark toggle is on but nothing appears on stream

If you're running a version from before Part 4, this was a real, known bug — the
old implementation only ever injected into the Chrome tab's DOM, which the encoder
never captured. It's fixed now; if you still don't see it:

- **Check it's actually saved and applied**: the Overlay Studio page's "Encoder
  Watermark" section shows a live badge (`LIVE: WATERMARK ON`/`OFF`) reading the
  *running* encoder's actual state, separate from what's saved. If they disagree,
  a banner tells you to restart the encoder — saving alone never restarts it.
- **Text not showing**: confirm `WATERMARK_TEXT` isn't empty and a font is
  selected. Font files live at `scripts/fonts/*.ttf` on the VPS — if missing,
  `stream.sh`/`test-recording.sh` log a warning and skip the watermark rather than
  failing the whole encode; check `logs/ffmpeg.log`.
- **Image not showing**: confirm the upload succeeded (Overlay Studio shows the
  current filename) and `WATERMARK_IMAGE_PATH` in `current-source.env` points at a
  real file under `/home/zoombot/zoom-stream/watermarks/`.
- **Verify safely without touching the live stream**: run a short local test —
  `sudo -u zoombot XDG_RUNTIME_DIR=/run/user/$(id -u zoombot) TEST_DURATION=8
  /home/zoombot/zoom-stream/scripts/test-recording.sh` — then pull the resulting
  `.mp4` off the box and look at a frame. This uses the exact same filter-building
  code as the real broadcast (`scripts/lib.sh`'s `build_watermark_filter()`)
  without ever touching `ffmpeg-stream`.

## Vault won't unlock / "Wrong master password"

The AEAD authentication tag failed to verify — either the password is actually
wrong, or the stored ciphertext is corrupted. There's no way to distinguish those
two cases by design (see [encryption.md](encryption.md) — that's what makes offline
password-guessing infeasible). If you're certain the password is right and it still
fails, check `dashboard.db`/`secrets.enc.json` weren't partially overwritten by a
failed disk operation; if truly lost, `vault reset --confirm` is the only way
forward.

## Dashboard didn't pick up new code after an upgrade

See [upgrade.md](upgrade.md)'s note on this — `systemctl restart dashboard` by hand
and re-check `systemctl status dashboard`'s `Active: active (running) since ...`
timestamp.

## Setup script (`install.sh`) fails a preflight check

The check messages are meant to be self-explanatory (OS version, disk space,
architecture) — they fail *before* anything is installed, specifically so a bad
environment doesn't leave you with a half-installed system. Fix what it names and
re-run; `install.sh` is idempotent, so re-running after fixing one thing is always
safe.
