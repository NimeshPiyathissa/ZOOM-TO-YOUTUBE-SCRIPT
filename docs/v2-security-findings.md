# V2 security findings: stream key and broadcast authorization

Scope: what was found while building the V2 fix branch (`fix/broadcast-authorization-v2`), what the branch changes, and what
is deliberately left for you to decide. No key was printed, rotated or changed, and no production system was touched.

## 1. Broadcasting without authorization (highest priority)

| Finding | Fix on this branch |
|---------|--------------------|
| Several code paths could start `ffmpeg-stream` as a side effect: switching/joining a source while offline, the watchdog's auto-recovery, scheduled "go live", the legacy `/api/units/ffmpeg-stream/*`, `/api/pipeline/restart`, `/api/config/apply-restarts`, `manage.sh go-live`. | One gate: `control.unit_action` refuses `ffmpeg-stream` start/restart without an opaque, single-use, 60 s `broadcast.Grant` bound to the signed-in user, the purpose and the exact source revision. Every path above now either lacks a grant or is removed. |
| The gate lived only in Python; a direct `systemctl --user start ffmpeg-stream` (or a reboot with the unit enabled) would still stream. | `scripts/stream.sh` also requires the one-time permission (`BROADCAST_AUTH_*` in `current-source.env`), burns it under an atomic lock, and **exits 0** when it is missing, so `Restart=on-failure` can never heal a crash back to air. |
| A crash was silently "recovered" (restart loop). | A crash becomes `ERROR` with kept diagnostics; it needs a fresh Go Live. A session never survives a reboot (boot id check). |
| Scheduled go-live. | Disabled: schedules still prepare meetings/sources; they never start or stop a live broadcast. |

Residual: anyone with root on the VPS, or with `zoombot`'s user manager, can still run `ffmpeg` by hand; that is outside what the
application can prevent. Disabling the unit's boot autostart (`systemctl --user -M zoombot@ disable ffmpeg-stream`) is a separate,
approval-gated production step.

## 2. YouTube stream key exposure

| # | Exposure | Severity | State |
|---|----------|----------|-------|
| 1 | **argv**: FFmpeg 6.1 cannot read its output URL from a file or stdin, so the key is in `/proc/<pid>/cmdline` and `ps` for every local user. | Medium (needs local access) | **Mitigation prepared, not applied**: `scripts/harden-proc-visibility.sh` (`hidepid=2` with an exempt group, report-only by default). Not fully removable with this FFmpeg. |
| 2 | **Log files**: FFmpeg prints its destination (including the key) at startup into `ffmpeg.log` and rotated copies. | Medium-High (logs are copied, shared, uploaded) | **Fixed going forward**: stderr passes through `redact_stream_secrets`. **Cleanup prepared, not run**: `scripts/scrub-ffmpeg-log.sh` (dry-run first; no backup copy is made on purpose). |
| 3 | **Environment**: the process environment (`/proc/<pid>/environ`) carried `YT_STREAM_KEY`, `VNC_PASSWORD`, Zoom link/passcode, Telegram token. | Medium | **Fixed**: `stream.sh` unsets them before `exec ffmpeg`. |
| 4 | Dashboard responses | - | Already masked: only "configured / missing" is ever returned (`/api/broadcast`). |

**Key rotation:** because the key was visible in old logs and in argv for the whole life of the stream, treat the current key as
exposed to anyone who had local access or log access. Rotating it is your decision and is *not* done here (YouTube Studio ->
Stream settings -> reset key, then update it in the dashboard). Do it after the log scrub, or the old key stays in rotated logs
you may have already copied.

## 3. Other observations

* A stale hint on the Zoom page said a scheduled join "starts the encoder too". Corrected: joins never go on air.
* The previous `base.js` showed the Go Live confirmation twice (production has this bug); replaced by the single grant flow.
* `scripts/zoom-atspi.py` and `zoom_classify.py` contained raw backspace bytes where `\b` was meant (a webinar-detection regex never matched). Fixed.
* The Zoom clean-feed extension used a subtree `MutationObserver` and un-scoped CSS that hid parts of the pre-join page and made state detection report CONNECTING in a live meeting. Rewritten and scoped to `html[data-zs-cleanfeed="on"]`.

## 4. Second review (2026-11-08)

| Finding | Fix on this branch |
|---------|--------------------|
| The V2 key-redaction filter (`sed -u`) is line-based, but ffmpeg ends live progress lines with `\r`: every progress update was held until the next newline (often until ffmpeg exited), so `ffmpeg.log` looked frozen and the dashboard lost fps/bitrate/dropped-frame metrics during a healthy stream. Reproduced: 0 bytes in the log after 3 s. | `redact_stream_secrets` now uses perl with `\r` as the record separator (perl-base is on every Ubuntu install; confirmed present on production), output byte-identical apart from the masked key; `tr`+`sed` fallback. Regression test times it inside bash. |
| A one-off scheduled Zoom join (`join_at`) ran while LIVE and swapped the on-air picture with nobody there to confirm. | Skipped while the encoder is up; the timestamp is cleared, an alert is sent, an audit row is written. |
| "Switching changes what viewers see" was confirmed only in the browser; the Controls quick-switch had no live check at all. | `POST /api/sources/{id}/switch` returns 409 `confirm_on_air` while live unless the body carries `confirm_on_air: true`; every UI caller confirms first. |
| `scrub-ffmpeg-log.sh --apply` could run while ffmpeg was writing: the atomic rename leaves the encoder writing to the unlinked file (metrics vanish until the next start). gzip-rotated logs were skipped. | `--apply` refuses while any ffmpeg runs; `.gz` logs are counted and rewritten as gzip. |
| `scripts/create_youtube_broadcast.py` (manual helper) created broadcasts with `enableAutoStart: true`, i.e. YouTube goes public as soon as data arrives. | Set to `false`. |
| Deploy scripts: live check only at preflight; `install -o` left zoombot files group-owned by root; restore used a recursive `chown` over whole trees. | Live guard at preflight, before install and before restart; per-file owner/group/mode kept and restored; post-install SHA-256 of every file; pre-rollback snapshot. |

Residual (not fixable in the application): the key is still in `/proc/<pid>/cmdline` while ffmpeg runs (FFmpeg 6.1 cannot
read its output URL from a file); `harden-proc-visibility.sh` is the prepared mitigation. On Ubuntu, `hidepid=2` can affect
tools that inspect other users' processes (polkit, some monitoring); test it with `--apply` then `--revert` in a quiet window.
