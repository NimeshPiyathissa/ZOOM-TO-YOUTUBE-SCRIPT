# EduMix - real live session test checklist

Everything below needs a real MJPhysics login and, for part C onward, a real live class. Do it with the
YouTube encoder **stopped** and `EDUMIX_CAPTURE_ENABLED = False` (the shipped default). Record the result of
each line (PASS / FAIL / notes) in the table at the end.

## A. First login (no saved session)

1. Open `/edumix`. Paste a class URL. Press **Open class**.
2. The live view shows the real site's login. The page reads **Sign-in needed** and the Sign-in helper opens.
3. Click the phone field in the live view, type the number (directly, or via the helper). **Check:** the helper
   field empties after Send; the number appears only on the site, not in the dashboard's Audit/Logs.
4. The state moves to **Enter the OTP** and the helper retitles/reopens. Type the code.
5. **Check:** the page lands on a signed-in page, shows **Signed in**, then **navigates to your class by itself** (once).
6. Note the exact wording/route the site uses on its login and OTP screens - if the state label was wrong at any
   point (e.g. showed *Page not recognised*), save a screenshot and the URL path. The classifier keys off the
   site's own **Logout** control, phone/OTP field attributes and a few phrases (`app/edumix.py` `JS_PROBE`).

## B. Returning user / expired session

1. Press **Stop**, then **Open class** again. **Check:** goes straight to the class, no login.
2. Restart `browser-source.service` (or reboot the VPS). **Check:** session still valid (cookies persist in
   `~zoombot/.config/chrome-profiles/edumix`, mode 700).
3. Let the session expire (or log out inside the live view). **Check:** **Sign-in needed** / **Session expired**,
   the supervisor does **not** auto-navigate or restart, **Re-authenticate** works.
4. A lesson whose access window ended shows "Session Expired: REQUEST MORE TIME". **Check:** state is
   *Session expired*; the dashboard never presses that button.

## C. The live player (the inspection this whole project is waiting on)

With a class that is actually live, in the live view, then via DevTools (`ssh -L 9222:127.0.0.1:9222` and
`chrome://inspect`) or the Remote GUI, record:

1. What plays the video: a `<video>` in the page, an iframe (which host?), Zoom Web SDK, YouTube/Vimeo embed, HLS?
   Does `edumix.status().player` show `present: true`? Is the state **Playing** / **Buffering** / **Paused** correct
   when you pause and resume in the player?
2. Is playback **protected** (EME/Widevine, HDCP, "protected content" text)? The state must read **Protected
   playback**. **If it does, stop here: capture is not enabled for it, and nothing below applies.**
3. Autoplay: does it start without a click? (`--autoplay-policy=no-user-gesture-required` is set; a "click to
   start" overlay must be clicked by hand in the live view.)
4. Native aspect ratio and resolution. **Fit / Fill / Reset** in the preview frame: no stretching. **Player fit**:
   did it apply (top-level `<video>` only)? If the player is in a cross-origin iframe it will say "not applied".
5. Fullscreen on the preview frame; Reconnect while playing; Stop.
6. Network: unplug/disable the VPS network ~20 s. **Check:** *Network offline*, retries with growing gaps (see the
   status hint "Retry n in Xs"), recovers on its own, returns to *Playing*.
7. Kill Chrome (`pkill -f chrome-profiles/edumix`). **Check:** *Browser unreachable*, systemd restarts it, the page
   returns to the class within ~1 min, no duplicate Chrome processes (`pgrep -fc chrome-profiles/edumix`).

## D. Resource check on the VPS (during C)

* `top`/`pidstat`: dashboard process CPU stays low (a probe is ~15 ms CPU and runs at most once per 1.5 s however
  many tabs are open); Chrome RSS stable over 30+ minutes; **one** Chrome tree; no growth in
  `ls /proc/$(pgrep -f dashboard)/fd | wc -l`.
* Leave `/edumix` open on two devices for 10 minutes: no growth in either.

## E. Existing features (regression)

* Zoom: join (client and web), leave, rejoin, clean-feed, meeting-only still work.
* A webpage / YouTube source goes live to YouTube as before; `/studio`, `/remote`, schedules unaffected.
* With EduMix as the active source, **Go Live** (topbar, `/studio`, `/remote`, a schedule, Telegram) is refused with the
  EduMix message and `ffmpeg-stream` stays stopped. `systemctl --user start ffmpeg-stream` exits immediately
  (guard in `scripts/stream.sh`).

## F. Only after C shows an authorized, non-protected, capturable player

See "Steps to finish the EduMix-to-YouTube integration" in the QA report. Nothing in A-E enables broadcasting.

| Item | Result | Notes |
|---|---|---|
| A1-A6 first login / OTP | | |
| B1-B4 returning / expired | | |
| C1 player technology | | |
| C2 protected? | | |
| C3-C5 autoplay, fit, fullscreen | | |
| C6 network drop | | |
| C7 browser crash | | |
| D resources | | |
| E regression | | |
