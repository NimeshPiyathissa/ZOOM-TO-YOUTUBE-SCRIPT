# EduMix Live (MJPhysics) source

Third live source next to Zoom and web pages: the operator signs in to
<https://mjphysics.lk/> (an Edumix student site) on the real site, opens a class,
and watches the player and connection state from the dashboard (`/edumix`).

**Status: view-only.** Media capture and YouTube broadcasting for EduMix are
**disabled** (`config.EDUMIX_CAPTURE_ENABLED = False`) until a real live class has
been inspected and an authorized capture method is confirmed.

## How it works

```
/edumix page ──(status, open, login, reconnect, stop, fit)──▶ app/edumix_api.py
        │                                                        │
        │  live noVNC view of :99 (/vnc/ws, existing proxy)      ├─ app/edumix.py   URL validation, page-state
        ▼                                                        │                  classifier, backoff, redaction
  Chrome kiosk (browser-source.service, SOURCE_TYPE=edumix)      ├─ app/cdp.py      page probe / navigate (existing)
  isolated profile ~zoombot/.config/chrome-profiles/edumix       └─ supervisor      auto-navigate, reconnect, recovery
```

* `SOURCE_TYPE=edumix` runs through the existing `browser-source` unit with its own
  isolated, owner-only (`700`) Chrome profile. The shared stream profile and Google
  account profiles are never used.
* The page state (loading, sign-in needed, OTP, signed in, no live class, playing,
  paused, buffering, ended, session expired, protected playback, player/page error,
  network offline, browser unreachable) is read through DevTools by a generic probe.
  The probe reads route, visible form *types*, `<video>` playback state, iframe *hostnames*
  and `navigator.onLine` - never input values, cookies or storage.
* The player technology is **not assumed**. Add a player-specific adapter later behind
  `edumix.classify()`/`JS_PROBE`; the page, auth flow and supervisor don't change.

## Authentication (manual, by design)

1. Paste a class URL on `/edumix` and press **Open class**. Only `https://mjphysics.lk`
   (or a subdomain) is accepted - no other scheme, host, credentials, or port.
2. If the isolated profile already holds a valid session, the browser lands on the class.
3. Otherwise the site's own login appears in the live view. **You** type your phone number
   and OTP - directly into the view, or via the **Sign-in helper** dialog (keystrokes are
   sent to the browser one by one and the field is wiped; nothing is posted to the
   dashboard, stored or logged).
4. When the site reports you are signed in, the dashboard navigates to the pasted class
   (once per login episode; switch off with "Open the class automatically").

The session lives only in the Chrome profile on disk. The dashboard stores a single
*timestamp* of when the session was last seen valid. OTPs, passwords and cookies are never
handled by dashboard code. Nothing bypasses OTP, CAPTCHA, DRM/EME, HDCP or other access
controls; a protected player is reported as **Protected playback** and left alone.

## Website limitations (observed 2026-11-04, read-only, one logged-in account)

* The site is a JavaScript single-page app (`Edumix`, Databox Technologies); routes include
  `/dashboard`, `/live-class`, `/class/<id>/detail/<year>/<month>`.
* Recorded lessons have **time-limited access** ("Session Expired: REQUEST MORE TIME").
  The dashboard never presses that - requesting more time is an account action for you.
* `/live-class` listed "No classes found" at inspection time, so **the live player was not
  seen**. The site's scripts reference the YouTube iframe API, Vimeo's player, HLS
  (`.m3u8`) and Firebase; which of those a live class actually uses is unknown.
* Sessions can expire server-side at any time; the page then shows **Session expired** and
  the supervisor waits for you rather than guessing.

## Reliability

| Situation | Behaviour |
|---|---|
| Network drop / page or player error | Reconnect with exponential backoff (2 s doubling to 60 s, ±20 % jitter, 12 attempts), then stop and say so |
| Chrome crash | systemd restarts the unit; after 3 further failed checks the supervisor restarts it, then re-navigates to the class |
| Live view (VNC) drop | Auto-reconnect 1 s → 20 s cap; paused while the tab is hidden |
| Dashboard unreachable (browser side) | Status polling backs off 2 s → 30 s |
| Sign-in / OTP / expired access / protected playback | Never auto-acted on - shown to you |
| Operator presses **Stop** | Only `browser-source` stops; the supervisor will not resurrect it until you open a class |
| Switching to EduMix while the stream is live | Refused (it would put the login page on air) |

## Not verified yet (needs a real authenticated live session)

* First-time phone + OTP login against the real site, and returning-session reuse.
* Real expired-session behaviour and the exact wording the site uses.
* The live player: type, autoplay, aspect ratio, `<video>` accessibility, protected playback.
* Page-level Fit/Fill (`object-fit`) on the real player (works only for a top-level `<video>`).
* Audio, A/V sync, FPS/bitrate/dropped-frame health, YouTube broadcast, stream
  stop/reconnect on a real encoder - all depend on capture, which is disabled.
* Real-browser (VNC) behaviour on the VPS; automated tests here mock Chrome, systemd and ffmpeg.

## Deployment checklist (do not deploy without explicit approval)

1. Branch `feature/edumix-live` (checkpoint tag `checkpoint-pre-edumix-20261104`) reviewed and merged.
2. Run `pytest` in `dashboard/` - only the known pre-existing failures may remain.
3. Copy `scripts/browser-source.sh` to `~zoombot/zoom-stream/scripts/` (executable); confirm
   `sudoers`/`write-source.sh` accepts the new `EDUMIX_URL` key (it accepts any `[A-Za-z_][A-Za-z0-9_]*`).
4. Deploy the dashboard files (`app/edumix*.py`, `templates/edumix.html`, `static/js/edumix.js`,
   `static/css/edumix.css`, edited `app/{config,sources,control,main}.py`, `templates/base.html`); restart `dashboard.service`.
5. Confirm `~zoombot/.config/chrome-profiles` is `700` after the first EduMix start.
6. Smoke test with the stream **stopped**: open `/edumix`, paste a class URL, see the live view, sign in by hand.
7. Confirm `Go Live` is refused while EduMix is the active source, and Zoom / webpage sources still go live.
8. Enabling capture is a separate, explicit change (`EDUMIX_CAPTURE_ENABLED`) after a live class has been inspected.

## Security notes from the pre-live review

* **Session cookies at rest.** Chrome runs with `--password-store=basic` (no desktop keyring on this headless box),
  so the cookie database in `~zoombot/.config/chrome-profiles/edumix` is protected only by file permissions
  (`700`, owner `zoombot`) - Chrome's "basic" store is obfuscation, not strong encryption. The dashboard itself never
  reads or copies it. Treat the VPS disk/snapshots as sensitive; use full-disk encryption on the instance, and do not
  copy that profile off the box. A site-level "log out of all devices" revokes the session server-side.
* **What the page probe reads.** Route, visible form *types*, `<video>` playback state, iframe hostnames,
  `navigator.onLine`, and whether certain phrases (Logout, "session expired", ...) appear. It never reads input
  values, cookies or storage, and returns no page text. A unit test guards this.
* **What is logged.** Audit entries carry the URL *path* only. Phone/OTP typed via the helper go straight to the
  browser as key events; a browser test confirms they appear in no HTTP request, storage, console or DOM.
* **Capture gate has three layers:** `control.unit_action` (dashboard), `scripts/stream.sh` (where the encoder
  actually starts) and a state-aware `capture_status()` (protected playback can never enable capture even if the
  flag is later turned on). Enabling capture later must write `EDUMIX_CAPTURE_ENABLED=1` into the source env.

## Performance notes

* One DevTools probe costs ~15 ms of dashboard CPU and ~30 ms wall on a loaded 4 GB machine, and is shared
  (1.5 s cache) by every open page and the supervisor.
* `cdp.py` built a TLS context for every plain-http loopback call (~500 ms CPU each on a small machine); fixed in a
  separate commit (`verify=False`, behaviour unchanged). It benefits the existing YouTube controls too.
