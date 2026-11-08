# V2 live validation plan - private/unlisted YouTube test (prepared, NOT authorized yet)

Goal: prove on production that a broadcast starts only through Go Live + confirmation, stays stable (audio, framing, CPU), stops
cleanly and never restarts by itself. Duration ~30 minutes. Run only after the owner's explicit authorization.

## Preconditions
- [ ] Phase 1 (dashboard tests) passed.
- [ ] In YouTube Studio -> Go live -> Stream: the stream that uses this key is set to **Private** (or Unlisted), title "V2 test".
      Note whether **Auto-start** is on in Studio: with it on, YouTube goes live as soon as data arrives; with it off you must
      also press "Go live" in Studio. Either is fine for this test - write down which.
- [ ] Test content that is not a real class: a test Zoom meeting you host yourself (with your consent to the bot joining), or a
      webpage source (e.g. a test-tone/clock page). Prefer the Zoom test meeting so audio/framing are exercised.
- [ ] Nobody else is using the dashboard; no recording.
- [ ] Baseline numbers (SSH, read-only): `sudo bash /home/zoombot/zoom-stream/scripts/measure-cpu.sh 60 5` (idle) and
      record the key-bearing line count of `ffmpeg.log` (dry-run scrub; last known 20).

## Steps and expected results
| # | Action | Expected | Result |
|---|--------|----------|--------|
| L1 | Join the test meeting with **Join only** | Zoom joins; Overview stays **Off air**; no ffmpeg process | PENDING |
| L2 | Press **Go Live**, then **Cancel** | Nothing starts; state stays Off air | PENDING |
| L3 | Press **Go Live**, read the dialog (source + destination), **Confirm** | State STARTING -> LIVE within ~10 s; YouTube Studio shows data arriving (private) | PENDING |
| L4 | Watch Overview for 2 min | fps ~ configured FPS, speed ~1.0x, bitrate steady, **metrics refresh every few seconds and never show "stale"** (proves the CR-aware log filter) | PENDING |
| L5 | Dropped frames | `dropped` total small; recent-window figure ~0 at steady state; record both | PENDING |
| L6 | CPU, in a second SSH window: `sudo bash .../live-test-monitor.sh 10 30` and `sudo bash .../measure-cpu.sh 120 5` | Encoder < ~1.8 cores, total load < 3 of 4, speed >= 0.99x | PENDING |
| L7 | Audio: speak/play a tone in the meeting for 2 min; watch the /remote audio meter; listen on the private YouTube page with headphones | Meter moves, no clicks/gaps/robotic sound; lip-sync acceptable (YouTube adds ~10-20 s latency) | PENDING |
| L8 | Framing: Meeting-only **Fit** (set it BEFORE L3 - the crop is part of the encoder filter, so changing it while live needs an authorized restart) | Picture shows the meeting without Zoom chrome, letterboxed if needed; nothing cut | PENDING |
| L9 | Framing: switch to **Fill** while live | UI asks for an authorized restart and shows the measured crop warning; confirm -> RTMP drops ~5 s and returns; check what was cropped | PENDING |
| L10 | Switch source while live without confirming (e.g. tap another tile, then Cancel) | Nothing changes; server refuses an unconfirmed switch | PENDING |
| L11 | **Stop** | State -> Off air; ffmpeg process gone; YouTube Studio shows the stream ended | PENDING |
| L12 | Wait 2 minutes, re-check | Still Off air; `ffmpeg-stream` inactive; no new ffmpeg; no "restarted" in the log - **no automatic restart** | PENDING |
| L13 | Log check (SSH): dry-run scrub count | Same as baseline (20): the new run added **0** key lines; new lines show `live2/<redacted>` | PENDING |
| L14 | Leave the meeting (Leave button) | Zoom leaves; nothing else changes | PENDING |

## Abort criteria (press Stop immediately)
Wrong content on air, audio leaking private conversation, speed < 0.95x for > 1 minute, load > 3.5, or anything unexpected.
Stopping is always allowed and never needs a confirmation grant.

## Afterwards
- Set the YouTube test broadcast to remain Private or delete it.
- Report: the L1-L14 table, CPU numbers, dropped-frame figures, audio notes, screenshots of Fit/Fill.
- Nothing in this plan changes configuration, keys, units or files on the server.
