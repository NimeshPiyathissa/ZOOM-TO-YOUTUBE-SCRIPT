# Real Zoom meeting test (Zoom Broadcast Console, production)

Everything below needs a REAL, authorized meeting or webinar. Nothing here was verified on production yet: the Zoom client has
only ever been seen at its home screen during the hotfix work. Do not start a YouTube stream until step 0 is done, and use a
PRIVATE or UNLISTED test broadcast (or a test channel) for the first run.

## 0. Before the meeting (no stream yet)
- [ ] The meeting/webinar link is valid and you are allowed to broadcast it (webinar `tk=` links are per-registrant).
- [ ] Zoom page -> **Add a meeting** (or restore the deleted source, see the restore proposal) -> **Join meeting**. Status reaches *In the meeting*.
- [ ] Overview: `zoom` Active, `ffmpeg-stream` **Off air** (neutral, not red), `browser-source` Idle. EduMix capture still locked.
- [ ] Optional baseline (read-only): `sudo bash /home/zoombot/zoom-stream/scripts/live-test-monitor.sh 5 12` (or copy it from the repo `scripts/`).

## 1. Speaker view
- [ ] Zoom page -> View: **Speaker**. Remote screen shows the speaker filling the window.
- [ ] **Meeting only (full frame)** ON. Overview preview shows Zoom's top bar and bottom toolbar gone.
- [ ] **Measure now**: expect "No black borders" (or the real borders). Note the numbers: left/top/right/bottom px.
- [ ] Fit: whole meeting visible, thin bars only where the picture is not 16:9. Fill: no bars, some edge cropped. Faces never cut off in either.

## 2. Gallery view
- [ ] View: **Gallery** with 4+ tiles. Repeat Measure now; Fit must show every tile completely.
- [ ] Fill: check the outer tiles are not cut (expected loss is ~14 % of the width on a 16:9 screen; if tiles are cut, use Fit).
- [ ] Join/leave a participant: the layout changes, the framing must not clip anything.

## 3. Screen sharing
- [ ] Share a 16:9 slide, then a 4:3 slide (pillarbox), then a tall window (letterbox).
- [ ] Measure now each time. Pure-black bars are reported with their size; dark slides and dark video are NOT treated as border.
- [ ] **Detect black borders automatically** ON: within ~1 minute the saved area changes (Zoom page note "Using the measured area ..."), and the page offers **Restart stream to apply** (the encoder is never restarted by itself).
- [ ] If the black belongs to the shared content (e.g. a letterboxed video), confirm you can leave Auto off and nothing is hidden.

## 4. Black banner removal / Reset
- [ ] Resize or move the Zoom window (Remote GUI): the Overview preview still shows only the meeting area, no desktop background.
- [ ] **Reset framing**: Fit, Auto off, fixed trims (56 px top / 96 px bottom). Confirm the stored region is cleared (Measure now still works).
- [ ] Meeting only OFF: the raw Zoom window is shown (nothing hidden).

## 5. Go Live (deliberate, your click)
- [ ] Overview -> Go Live: with Zoom in the meeting there is **no** "Zoom is not in a meeting" warning (with Zoom at home screen there must be one).
- [ ] The YouTube Studio preview matches the dashboard preview (framing, no stretching, correct aspect).
- [ ] Press **Stop**: `ffmpeg-stream` returns to Off air and stays stopped. Press Go Live again, then stop it for good.
- [ ] Close the dashboard tab for 2 minutes, reopen: the stream state is still LIVE and accurate; the encoder did not restart (uptime kept counting).

## 6. Video quality (1080p30 @ 6000 kbps, preset veryfast)
- [ ] During 10 minutes run `sudo bash live-test-monitor.sh 5 120` on the VPS. Targets: fps 29.5-30, speed 0.98-1.02x, dup/drop not growing, restarts 0, bitrate ~5-6.5 Mbit/s.
- [ ] YouTube Studio stream health: no "bitrate too low/high" or dropped-frame warnings; resolution 1080p, 30 fps.
- [ ] High-motion moment (screen share with video playback): speed stays >= 0.98x. If it falls below, switch to 720p30 (Config page, 4000 kbps) and record why.

## 7. Audio / video synchronization
- [ ] In the meeting play a clap or a countdown video with a beep on the frame; watch the YouTube stream (>= 20 s delay is normal): the lip movement/clap and the sound must be within ~100 ms.
- [ ] Speak in the meeting (Zoom mic unmuted by the bot is NOT required): audio on YouTube is present, not clipped, no echo of the bot's own voice.
- [ ] Re-check after 10 minutes (drift) and after a participant joins/leaves.

## 8. CPU and RAM (use the monitor columns)
- [ ] Total load stays below 3.0 on 4 vCPUs, `memAvl` stays above 2000 MB, no swap in use (none is configured).
- [ ] Expected: ffmpeg ~0.8-1.0 core (80-100 %), Zoom ~0.25-0.5 core, dashboard < 5 %. Record the actual numbers next to this list.

## 9. Controls
- [ ] Mic/Camera buttons: state read back from Zoom's own toolbar (webinar attendees show "N/A (attendee)").
- [ ] Rejoin works; Leave (keep stream) puts the slate on; Leave & stop stops the encoder.

## Stop conditions (abort and report)
A growing drop counter, speed < 0.9x for > 1 min, `restarts` > 0, memory available < 1000 MB, or any picture clipping of faces/slides.
