# EduMix on the VPS: architecture, staging plan, backup/rollback, checklist

**Status: design + local code only. Nothing here has been deployed, installed, enabled or restarted anywhere.**
EduMix capture and YouTube broadcasting remain disabled (`EDUMIX_CAPTURE_ENABLED = False`).

## 1. The only confirmed target

`54.254.180.52` (zoom.missakaart.lk) is the **only** VPS this project may touch, and it is **production**.
`13.233.105.162` is not recognised by the owner and is **not** a target, a staging box, or an assumption anywhere.
Every step below that touches the VPS needs explicit approval first. Until then: read-only audits only.

## 2. Read-only audit of 54.254.180.52 (2026-11-04, no changes made)

| Item | Observed |
|---|---|
| OS / kernel | Ubuntu 24.04.5 LTS, 7.0.0-1014-aws, up 17 days |
| Instance | c5a.xlarge: 4 vCPU (AMD EPYC 7R32), not burstable |
| RAM | 7.8 GB total, ~1.4 GB used, ~5.3 GB page cache, **6.3 GB available, no swap** |
| Disk | 48 GB root, 9.1 GB used, 39 GB free |
| Load | 0.01 / 0.02 / 0.00 at the time of audit |
| Listening | `:80`/`:443` Caddy; `127.0.0.1:8000` uvicorn (dashboard); `127.0.0.1:6080` noVNC; `127.0.0.1:5900` x11vnc; `127.0.0.1:2019` Caddy admin; `:22` |
| System services | `caddy`, `dashboard` (+ SSM agent, user managers) |
| `zoombot` processes | a **Zoom client was running** (~37% of one core, ~630 MB, plus its webview helpers) and `Xvfb` |

**Not checked** (needs `sudo`, so I did not): the deployed application commit, the state of `zoombot`'s user units
(`xvfb`, `browser-source`, `ffmpeg-stream`, ...), the dashboard venv path, `.env`/DB contents, and whether a live
stream is running right now. A Zoom client being active suggests production may be in use: **assume it is**.

## 3. Can staging run on the same VPS?

**Functional staging: yes, with strict isolation. Performance/stability testing: no, not while production is live.**

Why functional staging is plausible: ~6 GB RAM and 39 GB disk are free, load is near zero, and the EduMix source
side is small (one Chrome ≈ 0.6–1 GB, worker capped at 300 MB).

Why it is risky on a shared box: same CPU, kernel, network and disk; **no swap**, so a runaway Chrome can trigger the
OOM killer, which may pick a production process; a live Zoom/ffmpeg session is CPU-sensitive (a 720p24 encode plus
Chrome measured ~67% of 400% earlier).

Isolation rules for a same-VPS staging stack (all required):
1. Separate Linux user (e.g. `zsstage`) with its **own** home, Chrome profile, SQLite DB (a *copy*, never the live one) and venv.
2. Different resources from production: Xvfb display `:98` (not `:99`), DevTools port ≠ production's, dashboard on a
   different loopback port (not 8000, 6080, 5900, 2019), units named `*-stage`.
3. **No Caddy/DNS/firewall change.** Reach staging only through an SSH tunnel to its loopback port.
4. systemd limits on every staging unit: `CPUQuota=100%`, `MemoryMax=1500M`, `Nice=10`, `OOMScoreAdjust=500` (staging dies first).
5. **No `ffmpeg-stream` and no RTMP/YouTube credentials in staging**; capture flag stays `False`.
6. Run only when no live class/stream is in progress, and abort if load > 2 or available memory < 3 GB.
7. Never touch `zoombot`, `dashboard`, `caddy` or their files.

If those can't be guaranteed, or for 1080p30 / 30-60 min stability / failure-injection testing, use an alternative:
**a temporary clone**: create an AMI/snapshot of the production volume (read-only for production; needs your approval,
costs a few cents/hour), launch a same-size c5a.xlarge in an isolated security group, test there, then terminate it.
This is the only way to get trustworthy performance numbers without any production risk.

## 4. Target architecture (VPS)

```
Dashboard UI ─▶ dashboard.service (control only) ─▶ SQLite  edumix_jobs  (durable intent + health)
                                                         ▲
                              edumix-worker.service (own process, systemd, auto-restart)
                                supervises ─▶ browser-source.service (Chrome on Xvfb)  [existing]
                                                         │
                  ffmpeg-stream.service [existing, separate; BLOCKED for EduMix while capture is disabled]
```
* `dashboard` restarts/closes do not stop the worker. The worker reads **intent** (`desired`) from the job row and
  writes **health** (heartbeat, state) back; a reopened dashboard shows `GET /api/edumix/job`.
* Set `EDUMIX_WORKER_EXTERNAL=1` in the dashboard environment when the worker is a separate service, so the
  dashboard does not also run its own supervisor. (Default off: behaviour is unchanged until you opt in.)
* The browser engine stays: the site is a JavaScript app, so a managed Chrome on the server is still required. What
  disappears is the *desktop* window: on the VPS it lives on Xvfb and is controlled by services, not by you.

### Reboot recovery policy (`jobs.recovery_decision`)
After a VPS restart a job is resumed **only if all** hold: `desired = running`, `resume_on_boot` was enabled for that
job, it was never stopped or completed (`stopped_by`/`completed_at` empty), and it is a source-only job.
A broadcast is **never** resumed automatically. Default for new jobs: not resumed. An explicit Stop clears resume.
A resumed browser still needs a valid site login; an expired session is left for you (no automatic re-login, no OTP).

### Local Windows development vs the VPS
Local mode runs the same code, but everything dies if the laptop sleeps or powers off. Only the VPS can outlive the
laptop, and **that has not been verified yet**: do not rely on it until the staging checklist below passes.

## 5. Backups (before any approved change)
Run on the VPS only after approval; each is read-only on production data:
* App tree: `sudo tar -C /home/dashboard -czf /home/dashboard/backups/app-$(date +%Y%m%d-%H%M%S).tgz app --exclude=app/venv`
* DB: `sudo sqlite3 <DATA_DIR>/dashboard.db ".backup '/home/dashboard/backups/db-<ts>.sqlite'"`
* Config/units: copy `.env`, `/etc/caddy/Caddyfile`, `~zoombot/.config/systemd/user/*.service`, `dashboard.service`
* Record: `git -C /home/dashboard/app rev-parse HEAD`, `systemctl is-active` of every unit, `pip freeze`.
* Store a copy off the box (your laptop). Never copy `.env`/DB into Git. (An existing frontend backup directory,
  `/home/dashboard/backups/`, was used for the 2026-10-29 Atrium deploy.)

## 6. Rollback
* Code: restore the backed-up app tree, then `sudo systemctl restart dashboard`.
* DB: the new `edumix_jobs` table is additive and ignorable; to remove it, restore the DB backup.
* New unit: `sudo systemctl disable --now edumix-worker`, delete the unit file, `daemon-reload`. Unset `EDUMIX_WORKER_EXTERNAL`.
* Verify after rollback: `/health` = ok, Zoom page, YouTube page, `systemctl --user` units unchanged vs the recorded list.

## 7. Deployment checklist (nothing below has been done)
- [ ] You approve the target and the staging approach (same-VPS isolated, or temporary clone).
- [ ] Backups taken and copied off-box; rollback tested on the clone first.
- [ ] Deploy to **staging only**; production untouched.
- [ ] Staging: login via console, manual OTP, open class, close dashboard tab -> worker keeps running, reopen -> state restored.
- [ ] Staging: kill Chrome -> worker recovers with backoff; stop worker -> systemd restarts it; Stop -> nothing resurrects.
- [ ] Staging: reboot test confirms the recovery policy above (default job NOT resumed; opted-in job resumed; broadcast never).
- [ ] Inspect a **real live class** (player type, protected or not). If protected or unverifiable: capture stays disabled.
- [ ] Measure CPU/RAM/FPS before and after on the clone; 30-60 min stability run.
- [ ] Regression: Zoom and YouTube pages/flows unchanged.
- [ ] Only then: your explicit approval for production; deploy in a quiet window; keep rollback ready.

## 8. What is implemented vs not yet (honest status)
Implemented and unit-tested locally with mocks: durable job table, recovery policy, source worker loop, systemd unit
file (uninstalled), `/api/edumix/job`, the corrected expired-state logic (class-access expiry vs login expiry, plus
two-probe confirmation), the event-driven player-only screencast preview (`app/screencast.py`, one shared DevTools
capture for all tabs, stops when the last viewer leaves, reversible player-only CSS) and the unified Studio panel
(`app/studio.py`, `/api/edumix/studio`).
Verified against a REAL headless Chrome (local fake player page only, 2026-11-05): ~11 fps / ~740 kbps at 15 fps 60%
1280 px, player-only makes the video fill the viewport and is fully reversible, capture task ends after the last
viewer leaves. `EDUMIX_CAPTURE_ENABLED` is still False.
**Not done:** the independent encoder pipeline (blocked on capture being disabled), any real-player verification, any
measurement of CPU/RAM on the VPS, any VPS test. The venv path in the unit file is a placeholder to be confirmed
from the VPS during approved staging.
