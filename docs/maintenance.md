# Fonts, browser, and Zoom client maintenance

Operational notes for keeping the pipeline's fonts, Chrome flags, and Zoom client
current. See `scripts/fix-fonts-and-browser.sh` for the idempotent script that
applies the font/browser setup below — safe to re-run on an existing box or a
fresh install.

## Fonts

**Problem it fixes:** a stock Ubuntu 22.04 install only ships Liberation fonts.
Zoom's native Qt UI (participant names, chat, reactions) and any
system-font-dependent rendering has no glyphs for Sinhala, Tamil, CJK, or emoji,
so that text renders as boxes/tofu. Browser sources (YouTube, Meet) are largely
unaffected since they embed their own web fonts.

**What's installed:** `fonts-noto-core`, `fonts-noto-cjk`, `fonts-noto-color-emoji`,
`fonts-liberation`, `fonts-liberation2`, `fonts-dejavu`, `fonts-lklug-sinhala`, and
`ttf-mscorefonts-installer` (EULA preseeded, non-interactive, 120s timeout, falls
back to `fonts-freefont-ttf` if the sourceforge download fails/is blocked).

Installed with `--no-install-recommends` deliberately — the plain `fonts-noto`
metapackage's Recommends pull in `fonts-noto-cjk-extra` (214MB) and
`fonts-noto-extra` (334MB), ~550MB of glyph weights most installs don't need.

`/etc/fonts/local.conf` sets the default family aliases (sans-serif → Noto
Sans/Liberation Sans, serif → Noto Serif, monospace → DejaVu Sans Mono, emoji →
Noto Color Emoji) plus per-language fallback chains for Sinhala/Tamil/CJK, and
tunes rendering (antialias on, hintslight, rgba=none — best for a
screen-captured/streamed feed rather than a physical subpixel display).

**Verify:**
```bash
fc-list | wc -l                  # should be 400+
fc-match :lang=si                # -> Noto Sans Sinhala
fc-match :lang=ja                # -> Noto Sans CJK JP
fc-match emoji                   # -> Noto Color Emoji
```

**Re-apply / fresh install:**
```bash
sudo bash /home/zoombot/zoom-stream/scripts/fix-fonts-and-browser.sh
```

## Browser (web sources)

Web sources (YouTube, Google Meet, etc.) run **Google Chrome stable**
(`google-chrome-stable`, apt repo, not snap) via `scripts/browser-source.sh`,
launched by `browser-source.service`.

Current flags (see the script for the authoritative list):
- `--kiosk --app=<url>` — single-page kiosk, no chrome UI
- `--window-position=0,0 --window-size=<RESOLUTION>` — matches Xvfb's resolution
  exactly (both come from `RESOLUTION` in `.env` — bump both together)
- `--force-device-scale-factor=1.0 --high-dpi-support=1`
- `--disable-gpu` — chosen over `--use-angle=swiftshader
  --enable-unsafe-swiftshader` after benchmarking both with an identical
  canvas-render workload: swiftshader averaged ~10.4% aggregate CPU,
  `--disable-gpu` averaged ~2.7%. Xvfb has no real GPU, so full software
  compositing beats routing through a software GL implementation.
- `--lang=en-US --password-store=basic`
- `--disable-features=Translate,TranslateUI,Notifications`
- `--autoplay-policy=no-user-gesture-required`, `--noerrdialogs`,
  `--disable-infobars`, `--disable-session-crashed-bubble`, `--disable-translate`,
  `--disable-notifications`, `--disable-popup-blocking`,
  `--overscroll-history-navigation=0`, `--no-first-run`,
  `--no-default-browser-check`
- Audio: `PULSE_SINK=zoom_out` env var routes Chrome's audio to the same null
  sink FFmpeg captures from.

Profile: persistent, at `~zoombot/.config/stream-chrome-profile` — logins and
cookies survive restarts.

**User-Agent:** left at Chrome's real default, auto-versioned by Chrome itself —
no override configured. Only add a UA override if you find a specific site
serving a degraded layout to the real UA, and if you do, generate it from
`google-chrome-stable --version` at launch time, never hardcode a version.
Overriding the UA can also trigger Google's "This browser may not be secure"
sign-in block, so try without one first.

### Known issue: YouTube "Sign in to confirm you're not a bot"

The YouTube webpage source can hit this wall instead of playing the target video —
a Google bot-check on the automated Chrome session (datacenter IP + fresh/
automation-shaped browser fingerprint), not a fonts/UA/window-size problem. Fix:
sign into a real Google account once via noVNC in the persistent Chrome profile —
it's remembered across restarts. There is no automated credential entry anywhere
in this codebase (see the comment at the top of `browser-source.sh`); this is done
by hand, same as Zoom's Google sign-in flow (`zoom-google-signin.sh`).

## Zoom client updates

Installed via `apt install ./zoom_amd64.deb` from
`https://zoom.us/client/latest/zoom_amd64.deb` — check the version first, since
reinstalling when already current is a no-op:

```bash
dpkg -l zoom | tail -1                     # installed version
curl -s https://zoom.us/rest/download?os=linux | grep -o '"version":"[^"]*"'  # latest
```

If they differ:
```bash
cd /tmp
sudo -u zoombot curl -fL -o zoom_amd64.deb https://zoom.us/client/latest/zoom_amd64.deb
sudo apt install -y ./zoom_amd64.deb
```
`apt install ./file.deb` (not `dpkg -i`) so any new dependencies resolve
automatically.

**Before updating**, back up config (`fix-fonts-and-browser.sh` does this
automatically once per day):
```bash
sudo mkdir -p /home/zoombot/config-backups/$(date +%Y%m%d)
sudo cp -a /home/zoombot/.zoom /home/zoombot/config-backups/$(date +%Y%m%d)/.zoom
sudo cp -a /home/zoombot/.config/zoomus.conf /home/zoombot/config-backups/$(date +%Y%m%d)/zoomus.conf
sudo chown -R zoombot:zoombot /home/zoombot/config-backups
```

`zoom.service` sets `QT_SCALE_FACTOR=1` and `QT_AUTO_SCREEN_SCALE_FACTOR=0` so
Zoom's Qt UI renders at a pinned scale of 1 on the Xvfb display instead of
letting Qt guess a DPI-based scale.

## Restarting the display chain

Restarting `xvfb.service` kills everything downstream (Openbox, the active
source, and x11vnc), so always check `ffmpeg-stream` first and restart in this
order:

```bash
Z="sudo -u zoombot XDG_RUNTIME_DIR=/run/user/$(id -u zoombot)"
$Z systemctl --user is-active ffmpeg-stream   # if "active", STOP — ask before continuing

$Z systemctl --user restart xvfb.service
$Z systemctl --user restart openbox.service
$Z systemctl --user restart audio-setup.service
$Z systemctl --user restart x11vnc.service
# then whichever source is active per current-source.env:
$Z systemctl --user restart browser-source.service   # SOURCE_TYPE=webpage (or zoom via web join)
# or
$Z systemctl --user restart zoom.service             # SOURCE_TYPE=zoom, client join
```
Verify each with `systemctl --user status <unit>` before moving to the next.
`ffmpeg-stream` and the dashboard are left alone unless you explicitly intend to
(re)start them.

## Rollback

**Fontconfig:** `sudo rm /etc/fonts/local.conf && fc-cache -f` (system and as
`zoombot`) restores stock Ubuntu font matching. Font packages themselves are
harmless to leave installed; remove with
`sudo apt-get remove fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji
fonts-liberation2 fonts-dejavu fonts-lklug-sinhala ttf-mscorefonts-installer` if
you want them gone entirely.

**Zoom config:** restore from the dated backup:
```bash
sudo systemctl --user stop zoom.service   # as zoombot, if running
sudo rm -rf /home/zoombot/.zoom /home/zoombot/.config/zoomus.conf
sudo cp -a /home/zoombot/config-backups/<date>/.zoom /home/zoombot/.zoom
sudo cp -a /home/zoombot/config-backups/<date>/zoomus.conf /home/zoombot/.config/zoomus.conf
sudo chown -R zoombot:zoombot /home/zoombot/.zoom /home/zoombot/.config/zoomus.conf
```
To drop just the Qt scale-factor env vars, remove the two `Environment=QT_*`
lines from `/home/zoombot/.config/systemd/user/zoom.service` and
`systemctl --user daemon-reload`.

**Chrome flags:** edit `--disable-gpu --lang=en-US --password-store=basic` and
`--disable-features=...` in `scripts/browser-source.sh` back to whatever you
prefer, then restart `browser-source.service`.
