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
        if job.id != "watchdog":
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


def start() -> None:
    scheduler.add_job(_watchdog, "interval", seconds=WATCHDOG_INTERVAL_SECONDS, id="watchdog")
    load_schedules()
    scheduler.start()
