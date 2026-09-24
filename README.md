# Zoom / Browser → YouTube streaming bot

Streams a **source** — a Zoom meeting, any web page, or a direct media feed
(HLS/RTMP/RTMPS/SRT/file) — to YouTube from your own VPS, with a full web
dashboard to configure, watch, and control it. Sized for **4 vCPU / 8GB, no GPU**
at **1080p30**.

**If you're the meeting host**, Zoom's own "Live on Custom Streaming Service"
sends audio/video straight to YouTube with better quality and no VPS — use that
instead. This project is for when you're only a participant. See
[Legal & ethical use](#legal--ethical-use) before you run it against a meeting
that isn't yours.

## Architecture

```
Xvfb :99 (1920x1080x24)
  -> Openbox (window manager, no compositor)
  -> producer, selected by the active source's type:
       zoom     - Zoom Linux client, or Zoom's own web client in Chrome
       webpage  - Google Chrome in kiosk mode (its own persistent profile)
       direct   - none: FFmpeg reads the source URL itself
  -> PipeWire: null sink "zoom_out" <- producer's audio output
  -> FFmpeg:
       zoom/webpage - x11grab(:99) + pulse(zoom_out.monitor) -> libx264/AAC
       direct       - the source URL directly -> copy or libx264/AAC
       (+ an optional drawtext/overlay watermark filter, either way)
     -> rtmps://YouTube
  -> x11vnc on :99, reached only through the dashboard's authenticated proxy
     or an SSH tunnel

Dashboard (FastAPI, separate "dashboard" service account)
  -> talks to the pipeline only through a narrow sudoers allowlist
     (exact scripts, exact systemctl unit/verb pairs - see docs/security.md)
  -> encrypted secret vault (argon2id + AES-256-GCM) for every real
     credential - see docs/encryption.md
```

Everything runs as systemd services under two dedicated, lingering service
accounts (`zoombot` for the pipeline, `dashboard` for the web UI) — no interactive
login required for either, and each survives reboots on its own.

## Dashboard pages

| Page | What it does |
|---|---|
| `/remote` | Live status, quick controls, the YouTube deck, embedded Remote GUI |
| `/zoom` | Saved Zoom meetings, join method, per-account Google sign-in |
| `/config` | `.env` settings, sources, watermark's encoder config |
| `/overlay` | Overlay Studio — text/image watermark, position/size/opacity |
| `/accounts` | Per-account Chrome profiles for Google sign-in |
| `/studio` | YouTube Live Studio Room — broadcast controls, telemetry, chat |
| `/vnc` | Remote GUI (embedded noVNC), proxied through your dashboard session |
| `/logs` | Live-tailed, redacted service logs |
| `/schedule` | Scheduled join/go-live/stop |
| `/audit` | Who changed what, when |
| `/settings` | Recording, Telegram alerts, cloud storage config |

## Quick start

```bash
git clone <this-repo-url> zoom-stream
scp -r zoom-stream youruser@<vps-ip>:~/zoom-stream
ssh youruser@<vps-ip>
cd zoom-stream
sudo ./install.sh
```

`install.sh` runs preflight checks, installs everything (Xvfb, Openbox, PipeWire,
FFmpeg, the Zoom client, Google Chrome, Python), deploys the pipeline and
dashboard, and finishes with an interactive setup: a **Master Encryption
Password** (see [docs/encryption.md](docs/encryption.md)), a dashboard admin
login, and the VNC password. It ends with a summary panel — server IP, dashboard
URL, ports, config/log locations. **That summary may contain secrets if you set
them interactively; don't paste it anywhere, including into a chat with an AI
assistant.**

Non-interactive install (for automation): set `ZOOMBOT_MASTER_PASSWORD`,
`ZOOMBOT_ADMIN_USERNAME`, `ZOOMBOT_ADMIN_PASSWORD`, `ZOOMBOT_VNC_PASSWORD`,
`ZOOMBOT_UNLOCK_MODE` in the environment before running `install.sh` — same
validation rules, no defaults, nothing echoed.

Then, from your own machine:

```bash
ssh -i "<path to your key>" -N -L 8443:127.0.0.1:8443 <ssh-user>@<vps-ip>
```

Open `https://127.0.0.1:8443` (accept the self-signed certificate warning once).
See [docs/remote-access.md](docs/remote-access.md) for Tailscale/Caddy
alternatives, especially for phone access.

### First run, before you ever go live

1. Sign in to the dashboard, add a source (`/zoom` or `/config` → Sources).
2. Connect over the dashboard's Remote GUI (or an SSH-tunneled VNC client) and
   confirm the source actually looks right — meeting joined, mic/camera off,
   window maximized.
3. Set your YouTube stream key (YouTube Studio → Go Live → Stream → **Unlisted**
   visibility, set *before* going live — 1080p30, Normal latency).
4. Run a local test first: `scripts/test-recording.sh` records 60s to a local
   `.mp4` without ever touching YouTube — copy it off the box and watch it.
5. Go live from `/remote` once the local test looks and sounds right.

Full step-by-step detail, including the join/rejoin policy and Google sign-in
flow, is on the `/zoom` and `/remote` pages themselves.

## Sources

Configuration → Sources, or the switcher on `/remote`. Three types:
**Zoom meeting** (link, fallback passcode, bot name, guest or Google sign-in),
**Web page** (any http/https page — Google Meet, Teams, Webex, a slideshow —
opened in a Chrome kiosk with a persistent profile so logins survive restarts),
**Direct media** (an HLS/RTMP(S)/SRT/file URL FFmpeg reads directly — stream-copy,
no re-encode, when `ffprobe` confirms YouTube-compatible codecs). Adding a source
auto-detects the type from the URL. Switching sources is a quick reconnect (a few
seconds of buffering), not a hot swap — see `docs/configuration.md`.

## Watermark

Overlay Studio (`/overlay`) → "Encoder Watermark" section. Text or image, a full
9-point position grid, pixel margins, size, opacity — burned directly into
FFmpeg's filter graph, so it's real for every source type, not a browser-only
preview. Toggling requires restarting the encoder to take effect; the UI shows
you the live encoder's actual state and tells you when a restart is needed rather
than silently doing nothing. See `docs/troubleshooting.md` if it's not appearing.

## Security & secrets

Every real credential (stream key, Zoom passcode, VNC password, Telegram tokens)
lives in an encrypted vault — argon2id key derivation, AES-256-GCM authenticated
encryption, never a plaintext `.env` copy. Full design, the two unlock-mode
trade-offs, how to change the master password, and exactly what an attacker with
root can and cannot obtain: **[docs/encryption.md](docs/encryption.md)**.

Network exposure, the cross-user sudoers boundary, URL-injection defenses, and why
a public setup script can't meaningfully be password-gated:
**[docs/security.md](docs/security.md)**.

## Performance

`libx264 veryfast` at 1080p30/6000kbps typically uses ~2.5–3.5 of 4 vCPUs on a
dedicated-core VPS. If `monitor.sh` shows `speed=` sustained below `1.0x`, drop to
720p (`RESOLUTION=1280x720`, `VIDEO_BITRATE=4000`) before anything else. On
burstable/shared vCPU plans, expect to need this more often — CPU steal from noisy
neighbors is the most common cause of dropped frames on cheap VPS plans.

## Docs

- [docs/encryption.md](docs/encryption.md) — the vault: KDF, cipher, unlock
  modes, changing the master password, recovery, threat model
- [docs/configuration.md](docs/configuration.md) — every `.env` key, sources,
  watermark config
- [docs/security.md](docs/security.md) — network exposure, sudoers boundary, URL
  security, why the setup script can't be gated
- [docs/remote-access.md](docs/remote-access.md) — SSH tunnel, Tailscale, Caddy
- [docs/troubleshooting.md](docs/troubleshooting.md) — audio/video/watermark/vault
  issues
- [docs/upgrade.md](docs/upgrade.md) — `install.sh upgrade`/`uninstall`
- [docs/maintenance.md](docs/maintenance.md) — fonts, Chrome flags, Zoom client
  updates, restart ordering, rollback

## Legal & ethical use

If you're a participant (not the host), **make sure the host and other
participants know the meeting is being streamed before you go live** — most
platforms' and many organizations' policies require this, and recording/streaming
a meeting without consent may be illegal in your jurisdiction regardless of
platform policy. This tool automates nothing about *joining* beyond what a human
participant could already do by hand (deep-linking into a meeting, or using
Zoom's own web client) — it does not defeat waiting rooms, host approval, or
authentication. Use it only for meetings you have a real right to be in and to
share.

## Known limitations, honestly

- **Fresh-VPS installer verification**: `install.sh` has been reviewed carefully
  and syntax-checked, but has not yet been run end-to-end on a genuinely fresh
  VPS by an independent tester as of this release. If you hit an installer bug on
  a truly clean box, please open an issue with the exact error.
- **Source switching** is a quick reconnect (brief buffering on YouTube's side),
  not a zero-gap hot swap — a true hot swap would need an always-on local relay,
  a meaningfully bigger piece of infrastructure this project deliberately doesn't
  add.
- **Cached unlock mode** (the default) protects secrets against a stolen disk or
  backup, not against an attacker who already has root on the live box — see
  [docs/encryption.md](docs/encryption.md)'s threat-model table.

## Contributing / license

MIT — see [LICENSE](LICENSE). Issues and PRs welcome; `dashboard/requirements-dev.txt`
has the lint/test tooling CI runs (`ruff`, `pytest`, `shellcheck`).
