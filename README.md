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

Then, from any browser: open `https://<vps-ip>` and log in with the admin
username/password you just set — **no SSH tunnel, no port-forward command,
nothing to leave running**. Accept the one-time self-signed certificate warning
(unavoidable without a real domain — Let's Encrypt can't issue a cert for a bare
IP; see [docs/remote-access.md](docs/remote-access.md) if you want a
browser-trusted cert via a domain + Caddy instead).

This means the login page is directly reachable from the internet, protected by
TLS plus an account lockout after 5 failed attempts — not by network-level
obscurity. See [docs/security.md](docs/security.md) if you'd rather go back to
loopback-only + SSH tunnel (the more locked-down alternative, at the cost of the
tunnel command this setup avoids by default).

### First run, before you ever go live

1. Sign in to the dashboard, add a source (`/zoom` or `/config` → Sources).
2. Connect over the dashboard's Remote GUI and confirm the source actually looks
   right — meeting joined, mic/camera off, window maximized.
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

## Google account verification (`/accounts`)

**What it proves:** that a given Chrome profile *on the VPS* — the one that
actually plays age-restricted/sign-in-required YouTube videos and backs Zoom's
"Sign in with Google" — currently has a live, signed-in Google session, and that
the session is for the identity that profile is expected to hold. Sign-in itself
is always done by a human, by hand, over noVNC; nothing here ever sees a
password, 2-Step code, cookie, or token. "Verify now" drives that profile
(headless Chrome, the profile's own cookies) to `myaccount.google.com` — a page
that only renders identity for a signed-in session, redirecting anyone else to
Google's own sign-in page — and reads the account name back from the page.

Four outcomes, never collapsed into a binary: **Verified** (signed in as the
expected identity), **Wrong account** (signed in, but as a *different* Google
account than this profile is expected to hold — pinned from that account's own
first successful verify), **Signed out**, **Couldn't verify** (the check itself
failed to reach a conclusion — network timeout, an unexpected page, a security
challenge). A verified session older than 12 hours (twice the 6-hour background
recheck interval) shows amber even though the last check succeeded, so a lapsed
session surfaces here before it breaks a live stream, not during one. The same
state is what gates the account pickers on `/zoom` and `/remote` — there's one
shared list, not a per-page copy.

**Why this isn't Google OAuth**, deliberately: an OAuth flow run in an admin's
own browser on their own laptop proves they own a Google account. It proves
nothing about whether the VPS profile — a completely different browser, on a
different machine, with its own separate cookie jar — actually has a session.
An OAuth-based badge could read green while the VPS profile is signed out: a
false green, discovered only when a stream fails. See the "why session
verification" note at the top of `app/accounts.py` for the full reasoning.

**What "Verified" actually combines:** each account card runs *two*
independent checks and only shows green once both agree, when both apply —

1. **Browser session** (above): does the VPS Chrome profile have a live
   Google session, for the expected identity?
2. **YouTube Data API link** (below): is there a *connected* OAuth token, for
   the *same* Google account as this profile's identity?

If the API is connected but for a different email than this profile, the card
shows **API: wrong account** in red. If the browser session is signed out (or
wrong-account/couldn't-verify) while the API connection for that same identity
is otherwise fine, the card shows **API connected · browser signed out** in
amber with a Re-authenticate button — the API being connected is never, by
itself, shown as green, because Zoom joining and YouTube playback both need the
*browser* session specifically. An account that simply isn't the one the API
happens to be connected to just shows the API row as "not connected" /
"not set up" without blocking its own green badge — the API check only matters
for the account it's actually linked to by matching emails.

## YouTube Data API connection (also on `/accounts`, its own card)

A separate, genuinely OAuth-shaped feature from the section above: an
application-level connection that lets the dashboard call the YouTube Data API
on behalf of one YouTube channel — creating unlisted broadcasts and reading
their stream health. This is **not** a signed-in Chrome profile and has no
effect on which account a meeting or webpage source joins as; that's still
entirely the accounts list above it.

> **Root-cause note:** this card's Client ID/Client Secret fields previously
> had no server-side format validation and sat in a plain text+password pair
> that some browsers' password managers will autofill into *any* such pair on
> a page, form or no form. A dashboard login autofilled here once got saved as
> the OAuth client (`client_id="admin"`, secret = the admin password),
> producing Google's `invalid_client` error on every Connect attempt. Both
> holes are closed now: the Client ID is validated server-side against
> `^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$` before it's ever saved
> (reject, don't redirect to Google with it), and the two fields carry
> `autocomplete="off"`/`"new-password"`, non-login `name`/`id`s, and a decoy
> username/password pair placed right before them to absorb the autofill
> heuristic instead (an earlier `readonly`-until-first-interaction layer was
> tried too but broke pasting on some browsers, so it was dropped — the
> decoy pair plus server-side validation are the load-bearing defenses). If
> you set this up
> before this fix, re-check the Client ID/Secret you saved — if they weren't
> saved via a genuine Google Cloud Console copy-paste, re-enter them, and
> rotate your dashboard password regardless, since this means it was written
> into the secret vault (encrypted at rest, but still — rotate it).

**Setup:**

1. Requires the Caddy + real-domain HTTPS setup (`docs/remote-access.md`) —
   Google rejects a bare-IP redirect URI outright.
2. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or reuse one), enable the **YouTube Data API v3**, configure the
   OAuth consent screen (**External**, Testing is fine — do not choose
   Internal/`org_internal` unless you're on a Google Workspace org and want it
   restricted to that org), and add your own Google account under **OAuth
   consent screen → Test users**.
3. Create an **OAuth client ID** (type: **Web application**) and add this
   exact **Authorized redirect URI** (also shown on the `/accounts` page
   itself, with a copy button), byte-for-byte including scheme and no trailing
   slash: `https://<your-domain>/api/youtube/oauth/callback`
4. On `/accounts`, under "YouTube Data API", paste in the Client ID and
   Client Secret from that OAuth client and **Save** — a malformed Client ID
   (not matching the pattern above) or an empty Secret is rejected inline,
   right there, before anything is stored or any request reaches Google; a
   Secret that doesn't start with `GOCSPX-` (the current Google format) saves
   but shows a warning, since some older still-valid secrets don't have it.
5. **Connect** — this opens Google's consent screen in your own browser tab
   (never noVNC; there's no "which profile" question here, only "which
   channel"), using PKCE (S256) and a random `state` bound to both the PKCE
   verifier and your dashboard session server-side, so completing someone
   else's half-started flow isn't possible. `access_type=offline` +
   `prompt=consent` are always forced so Google reliably hands back a refresh
   token even on a repeat consent.
6. If your OAuth consent screen is still in **Testing** mode, Google expires
   the refresh token after 7 days regardless of use, and only lets pre-added
   test users connect at all — either add yourself as a test user (step 2) and
   reconnect weekly, or publish the app (no Google review is required unless
   you request sensitive/restricted scopes; the `youtube.force-ssl` scope used
   here does require verification for a *published, public* app, but Testing
   mode with your own account added as a test user works indefinitely for
   personal use — see the in-app note on the card too).

**What's stored where:** Client ID/Secret and the refresh token live in the
same encrypted vault as every other secret in this app (Part 0) — never in
plaintext, never returned to the browser; the Secret field is write-only, and
after saving the card shows only "configured, ends in ••••xxxx" (the last 4
characters, kept as a non-secret hint alongside everything else below). The
connected channel's id/title, the connected Google account's email (shown
masked, e.g. `n•••e@gmail.com`, used server-side to match this connection to
the right account card above), granted scope, and connection status live in
the small settings table alongside everything else non-secret. The short-lived
access token is kept in memory only, for the life of the dashboard process.

**Status states:** *Not connected*, *Connected* (shows the connected channel's
title and masked identity email), and *Needs reconnecting* — Google rejected
the stored refresh token (revoked from your Google Account, the Testing-mode
7-day grant expired, or the consent screen was reconfigured). Needs
reconnecting clears the useless refresh token automatically rather than
silently retrying it; only a fresh Connect fixes it. **Test connection** forces
a real refresh-token grant *and* a real `channels.list` call right now, rather
than trusting a cached badge, and reports the channel name back.
**Disconnect** revokes the token with Google and clears it from the vault.

**Google error messages, decoded:** `invalid_client` (the Client ID/Secret
pair itself is wrong — re-check both in Cloud Console), `redirect_uri_mismatch`
(the URI above isn't registered exactly), `access_denied` (consent was
declined, or — very commonly — your account isn't added as a test user yet),
`org_internal` (the consent screen is restricted to an internal Workspace org
— switch its User type to External), `invalid_grant` (the refresh token was
revoked or expired — shows as *Needs reconnecting*), `quotaExceeded` (the
YouTube Data API's daily quota is used up — try later, nothing to reconnect),
and a connected token missing the `youtube.force-ssl` scope (disconnect and
Connect again). Every one of these surfaces as plain text on the card or as a
toast — never a bare Google error code.

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

### Icon licensing

The dashboard's sidebar icons come from [Hugeicons](https://hugeicons.com)' free
"Stroke Rounded" set (`@hugeicons/core-free-icons`), MIT licensed. Only that free
tier is used — Hugeicons' larger Pro library (60,000+ icons, 10 styles) is a separate
commercial product whose license forbids redistributing the source files, so none of
it is bundled here. Path data is copied in as inline `<symbol>` markup (see
`dashboard/templates/_icons.html`) rather than pulled from a CDN or npm dependency, so
the dashboard's script/style CSP doesn't need a third-party origin for icons. The rest
of the icon set is [Lucide](https://lucide.dev) (ISC), self-hosted the same way.
