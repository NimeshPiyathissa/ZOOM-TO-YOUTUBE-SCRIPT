"""Scheduled join/go-live/stop (Asia/Colombo), an auto-recovery watchdog
for the case systemd itself gives up (unit reaches 'failed' after its
own restart-limit is exhausted), and an optional webhook alert."""
from __future__ import annotations

import asyncio
import time
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, db
from . import sources as sources_mod
from . import control

scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))

WATCHDOG_INTERVAL_SECONDS = 20
ALERT_COOLDOWN_SECONDS = 300
MAX_RECOVERY_ATTEMPTS_PER_HOUR = 3

_last_alert_ts = 0.0
_recovery_attempts: list[float] = []
_prev_zoom_in_meeting = False


async def _send_alert(message: str) -> None:
    webhook = db.get_setting("webhook_url", "")
    if not webhook:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook, json={"text": f"[zoom-stream] {message}"})
    except Exception:
        pass


async def _watchdog() -> None:
    global _last_alert_ts
    try:
        show = control.unit_show("ffmpeg-stream")
    except control.ControlError:
        return
    if show["active_state"] != "failed":
        return

    now = time.time()
    if now - _last_alert_ts > ALERT_COOLDOWN_SECONDS:
        _last_alert_ts = now
        await _send_alert("ffmpeg-stream has failed and systemd exhausted its own restart attempts.")
        db.audit(None, "watchdog_alert", "ffmpeg-stream failed")

    if db.get_setting("auto_recovery", "off") == "on":
        recent = [t for t in _recovery_attempts if now - t < 3600]
        if len(recent) < MAX_RECOVERY_ATTEMPTS_PER_HOUR:
            _recovery_attempts.append(now)
            try:
                control.unit_action("ffmpeg-stream", "reset-failed")
                control.unit_action("ffmpeg-stream", "start")
                db.audit(None, "auto_recovery", "restarted ffmpeg-stream")
            except control.ControlError:
                pass

    # Telemetry and frame drop check
    try:
        from . import stats, telegram
        prog = stats.ffmpeg_progress(show)
        if prog:
            drop = int(prog.get("drop") or 0)
            frame = int(prog.get("frame") or 0)
            total = frame + drop
            if total >= 100:
                drop_pct = (drop / total) * 100.0
                if drop_pct > 5.0:
                    telegram.alert_high_dropped_frames(
                        drop, total, drop_pct,
                        fps=float(prog.get("fps") or 0.0),
                        bitrate_kbps=float(prog.get("bitrate_kbps") or 0.0),
                    )
    except Exception:
        pass

    # Zoom disconnect watchdog & BRB failover
    global _prev_zoom_in_meeting
    try:
        from . import telegram, slate
        if show.get("phase") == control.PHASE_LIVE:
            cur_source = control.read_current_source()
            if cur_source.get("SOURCE_TYPE") == "zoom":
                z_status = control.zoom_meeting_status()
                st = z_status.get("status")
                if st == "in_meeting":
                    _prev_zoom_in_meeting = True
                elif _prev_zoom_in_meeting and st in ("ended", "removed", "expired", "join_failed", "not_joined"):
                    _prev_zoom_in_meeting = False
                    detail = z_status.get("detail") or f"Status: {st}"
                    telegram.alert_zoom_disconnected(detail)
                    slate.set_brb_state(True)
                    await slate.push_brb_to_kiosk()
    except Exception:
        pass

    # Sync overlay and BRB holding state to kiosk
    try:
        from . import overlay, slate
        await overlay.push_overlay_to_kiosk()
        if slate.get_brb_state().get("active"):
            await slate.push_brb_to_kiosk()
    except Exception:
        pass


async def _run_scheduled(action: str, source_id: int | None) -> None:
    try:
        if source_id:
            s = sources_mod.get_source(source_id)
            if s:
                await asyncio.get_event_loop().run_in_executor(None, control.apply_source_config, s)
                sources_mod.set_active_source_id(source_id)
        if action == "go_live":
            control.pipeline_start()
        elif action == "stop":
            control.pipeline_stop()
        db.audit(None, f"scheduled_{action}", f"source_id={source_id}")
    except Exception as exc:  # noqa: BLE001 - scheduled job must never crash the loop
        await _send_alert(f"Scheduled {action} failed: {exc}")


async def _run_join_at(source_id: int) -> None:
    """One-off scheduled join from the Zoom page: switch to the meeting
    (same path as tapping its tile - keeps RTMP up where possible, and
    starts the encoder if it isn't running), then clear the timestamp so
    it never fires twice."""
    try:
        s = sources_mod.get_source(source_id)
        if not s or s["type"] != "zoom":
            return
        await asyncio.get_event_loop().run_in_executor(None, control.start_source, s)
        sources_mod.set_active_source_id(source_id)
        sources_mod.mark_joined(source_id)
        opts = dict(s.get("options") or {}); opts["join_at"] = None
        sources_mod.update_source(source_id, s["name"], s["type"], s["url"], opts, s.get("account_id"))
        db.audit(None, "scheduled_join", f"source_id={source_id}")
    except Exception as exc:  # noqa: BLE001 - scheduled job must never crash the loop
        await _send_alert(f"Scheduled Zoom join failed: {exc}")


def load_schedules() -> None:
    from apscheduler.triggers.date import DateTrigger
    from datetime import datetime
    for job in list(scheduler.get_jobs()):
        if job.id not in ("watchdog", "verify-accounts", "youtube-watch"):
            scheduler.remove_job(job.id)
    for s in sources_mod.list_sources():
        join_at = (s.get("options") or {}).get("join_at") if s["type"] == "zoom" else None
        if not join_at:
            continue
        when = datetime.fromtimestamp(float(join_at), ZoneInfo(config.TIMEZONE))
        if when.timestamp() < time.time() - 300:
            continue  # stale (dashboard was down when it was due) - shown as overdue on the page, never auto-fired late
        scheduler.add_job(_run_join_at, DateTrigger(run_date=when), args=[s["id"]],
                          id=f"join-at-{s['id']}", replace_existing=True)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
    for row in rows:
        trigger = CronTrigger(
            hour=row["hour"], minute=row["minute"], day_of_week=row["days_of_week"],
            timezone=ZoneInfo(config.TIMEZONE),
        )
        scheduler.add_job(
            _run_scheduled, trigger, args=[row["action"], row["source_id"]],
            id=f"schedule-{row['id']}", replace_existing=True,
        )


ACCOUNT_VERIFY_INTERVAL_HOURS = 6


async def _verify_accounts() -> None:
    """Every few hours, ask Google whether each account's session still
    exists (headless, or through the running kiosk for a profile it
    holds) so a lapsed session is flagged in the UI before a stream
    depends on it. Not more often: each check is a Chrome launch."""
    from . import accounts as accounts_mod
    try:
        results = await asyncio.get_event_loop().run_in_executor(None, accounts_mod.verify_all, "scheduled")
        lost = [r for r in results if r.get("state") in ("signed_out", "inconclusive")]
        if lost:
            await _send_alert("Google account needs re-authentication: " + ", ".join(r["label"] for r in lost))
    except Exception:  # noqa: BLE001 - never let a check kill the loop
        pass


YT_WATCH_INTERVAL_SECONDS = 4


async def _youtube_watch() -> None:
    from . import youtube_watch
    from .main import play_youtube_link
    try:
        await youtube_watch.tick(play_youtube_link)
    except Exception:  # noqa: BLE001 - never let a tick kill the loop
        pass


def start() -> None:
    from datetime import datetime, timedelta
    scheduler.add_job(_watchdog, "interval", seconds=WATCHDOG_INTERVAL_SECONDS, id="watchdog")
    scheduler.add_job(_verify_accounts, "interval", hours=ACCOUNT_VERIFY_INTERVAL_HOURS, id="verify-accounts",
                      next_run_time=datetime.now(ZoneInfo(config.TIMEZONE)) + timedelta(minutes=3))
    scheduler.add_job(_youtube_watch, "interval", seconds=YT_WATCH_INTERVAL_SECONDS, id="youtube-watch",
                      max_instances=1, coalesce=True)
    load_schedules()
    scheduler.start()
