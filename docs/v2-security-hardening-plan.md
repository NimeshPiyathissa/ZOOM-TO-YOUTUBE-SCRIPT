# V2 post-deployment security plan (prepared, NOT executed)

Production: zoom.missakaart.lk (54.254.180.52). Each part below needs its own explicit approval. All of them require the
encoder to be stopped (Overview: Off air) and none of them restarts the encoder, Zoom or the browser source.
Recommended order: **A (read-only survey) -> B (scrub) -> C (rotation) -> D (restart policy) -> E (key rotation, your decision)
-> F (command line, decision only)**. Scrub before enabling rotation, or the old keys get copied into compressed archives.

## A. Read-only survey (no approval needed beyond read-only access)
| Check | Command (prints counts/names only) |
|---|---|
| Key lines per log | `sudo bash /home/zoombot/zoom-stream/scripts/scrub-ffmpeg-log.sh` (dry run is the default: counts only, exit 1 = keys found) |
| Logs inside backup archives | `for t in $(sudo find /home/dashboard/backups -name '*.tgz'); do sudo tar -tzf "$t" | grep -q 'ffmpeg.log' && echo "$t"; done` |
| Journals | `sudo journalctl _UID=$(id -u zoombot) -q | grep -cE 'rtmps?://[^/ ]+/[^/ ]+/[^< ]'` (last result: 0) |
| Other copies | the 2026-11-08 pre-flight found 20 key lines in `logs/ffmpeg.log` (17 MB), 0 in journals, 0 real keys in backups |

## B. Remove historical stream keys from ffmpeg.log
- **What:** `scrub-ffmpeg-log.sh --apply` rewrites `ffmpeg.log` (and `ffmpeg.log.*`, including `.gz`) replacing only the key part of
  each `rtmp(s)://` URL with `<redacted>`. Same owner and mode; atomic rename; **no backup copy is made on purpose** (a backup
  would keep the secret).
- **Safety:** it refuses while any ffmpeg runs (encoder or recording). A file that changes during the rewrite is skipped.
- **Steps:** 1) Off air confirmed. 2) Dry run -> note the count (expected 20). 3) `sudo -u zoombot bash .../scrub-ffmpeg-log.sh --apply`.
  4) Dry run again -> expect `clean`, exit 0. 5) `stat` shows `zoombot:zoombot 640` unchanged.
- **Rollback:** none needed and none possible by design (only the key text is removed; every other byte is kept).
- **Impact:** none on services. The dashboard Logs page keeps working.

## C. Log rotation for ffmpeg.log
- **What:** a new file `/etc/logrotate.d/zoom-stream` (root, 644):
  ```
  /home/zoombot/zoom-stream/logs/ffmpeg.log {
      su zoombot zoombot
      weekly
      maxsize 50M
      rotate 4
      compress
      delaycompress
      copytruncate
      missingok
      notifempty
  }
  ```
- **Why copytruncate:** ffmpeg and the redaction filter keep the file open in append mode (`>>`), so truncating in place is
  safe while live (writes continue at the new end) and nothing needs to be signalled or restarted.
- **Steps:** after B. 1) Write the file. 2) `sudo logrotate -d /etc/logrotate.d/zoom-stream` (debug: shows what it would do,
  changes nothing). 3) Leave the daily logrotate timer to apply it; optionally `sudo logrotate -f` once while Off air.
- **Rollback:** delete the file. Rotated copies stay in `logs/` as `ffmpeg.log.N(.gz)`.
- **Impact:** none on services; the dashboard reads only the live `ffmpeg.log` tail.

## D. FFmpeg restart-policy hardening
- **Current:** the unit has `Restart=on-failure`. Since V2, `stream.sh` refuses any start without a one-time Go Live permission and
  exits 0, so a crash can no longer go back on air. This step is defence in depth: it makes the unit itself say so.
- **What:** `optional-encoder-unit-no-restart.sh apply` (in the V2 bundle) adds the drop-in
  `~zoombot/.config/systemd/user/ffmpeg-stream.service.d/v2-no-restart.conf` with `Restart=no`, then `daemon-reload` for the
  zoombot user manager only. It does not start, stop or restart anything.
- **Steps:** Off air. `status` -> `apply` -> `status` (expect `Restart=no`, drop-in present, boot `disabled`).
- **Rollback:** `optional-encoder-unit-no-restart.sh revert`.
- **Trade-off (already true in V2):** a dropped connection ends the broadcast; the operator presses Go Live again.

## E. Stream-key rotation (your decision)
Do it after B (and C), so no log still holds a valid key: YouTube Studio -> Stream settings -> reset/create key -> paste it in the
dashboard's stream-key field (masked). Then a private test (Phase 3). Old key stops working immediately; any external copy is dead.

## F. Command-line exposure (decision record)
- FFmpeg 6.1 needs the destination URL, key included, on its command line, so `/proc/<pid>/cmdline` shows it while live.
- **Who can actually read it on this box:** root/`ubuntu` (has sudo, can read `.env` anyway), `zoombot` (owns `.env`), and
  `dashboard` (needs `/proc` for encoder CPU metrics, and already has 79 sudo rules to act as zoombot). `caddy`/`syslog` and
  other service accounts could read it.
- **`harden-proc-visibility.sh` (hidepid=2 + exempt group):** hides other users' processes from accounts NOT in the exempt
  group. Because `dashboard` must be exempt (metrics) and `ubuntu`/root can read everything anyway, the gain is limited to
  service accounts such as `caddy`. It needs a dashboard restart and can disturb tools that list other users' processes
  (polkit/logind helpers, monitoring). **Recommendation: low value here; do not apply unless a shared/multi-user use appears.**
- **Real fix (future, needs a package install):** a local RTMP relay (e.g. nginx-rtmp) on 127.0.0.1 holding the key in a root-only
  config; ffmpeg would then push to `rtmp://127.0.0.1/...` with no key in its arguments. Separate design and approval.
- **Practical mitigation now:** B + E (rotation), so the key that was exposed in logs is dead.
