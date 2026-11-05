# Zoom recovery hotfix (2026-11-05): button feedback, service status, measured framing

Branch `hotfix/zoom-controls-status`. Local only until deployed with explicit approval.

## What production showed (read-only audit, 54.254.180.52)
* `zoom.service` active, but the Zoom client was at its **home screen, not in a meeting** (status `not_joined`).
* The Zoom library was **empty and no source was active** (a `source_delete` is in the audit log); that is why **Join meeting is
  disabled**, and why mic/camera/view are inert (they need a meeting).
* `ffmpeg-stream` and `browser-source` were `inactive/dead` **by design** (nothing on air; the active source is the desktop client).
  The Overview showed the raw systemd words, which read as failures.
* All API paths the frontend calls exist on the production backend (static comparison) and the pages raise no console errors.
* 1080p30 capture+encode measured on the VPS: ~0.75 core (static screen), ~1.0 core worst-case synthetic motion, speed 0.99x,
  no drops. The Meeting-only crop adds no measurable CPU.

## Root causes and fixes
| Symptom | Cause | Fix |
|---|---|---|
| "I pressed Go Live and nothing happened" | `unit_action` returned `ok:false` as HTTP 200 and a start that died a second later looked like success | `_checked_unit_action`: errors become HTTP 400 with the reason; start/restart wait up to ~5 s and report a unit that stops again (stream key redacted) |
| Go Live with nothing to show | no guard | `GET /api/stream/preflight` + confirm dialog on every Go Live path (warning, never a block) |
| Services look "inactive" | raw systemd state shown | `app/status.py`: Active / On air / Starting / Restarting / Error / Stopped (needed) / **Idle** / **Off air** / Not configured / Disconnected; `units[].status` and `services[]` (dashboard, Caddy, EduMix worker) in `/api/state`; badge tooltips show reason + raw state |
| Zoom page "ZOOM.SERVICE LIVE" | phase word `LIVE` reused for "running" | now "running" |
| Inert mic/camera/view | `pointer-events:none`, no explanation | real `disabled` buttons with a tooltip |
| Join disabled silently | no source | tooltip: pick or add a meeting |
| Black bars from Zoom's own letterbox | fixed top/bottom trims only | `app/framing.py`: measured pure-black borders (Auto), Measure now, Reset; Fit/Fill apply to the measured area |
| Preview differed from the broadcast | preview showed the raw screen | `preview.meeting_only_filter()` twin of the encoder filter |
| "Restart to apply" forgotten after a dashboard restart | marker lived in memory | persisted in `settings` |

## Framing rules (what is and is not cropped)
Only pure black counts (luma <= 20 of 255); dark-grey UI and dark video are content. Several frames are unioned, borders under
8 px are ignored, 2 px of padding is kept, an area smaller than half the band is rejected, and the encoder ignores a region
smaller than a third of the frame. If black is part of the source video, leave Auto off - it is reported, not hidden.
**Fit** never crops (letterboxes the measured area). **Fill** crops the sides/top to remove bars: on a 16:9 screen with the
56/96 px chrome bands removed it trims ~14 % of the width - use Fit for gallery view or shared slides.
Not verified: any real Zoom meeting (speaker, gallery, screen share, window resize).

## Streaming independence (unchanged, verified by inspection)
`ffmpeg-stream` is a systemd *user* unit of `zoombot` (lingering): closing or reloading the dashboard cannot stop it; the
dashboard only calls `systemctl`. **Finding:** the unit is `enabled` (WantedBy=default.target), so after a VPS reboot it would
start by itself even if the broadcast had been stopped on purpose. Proposed fix (needs approval, not applied): disable boot
autostart for `ffmpeg-stream` and drop it from the reboot-readiness "enabled" check.
