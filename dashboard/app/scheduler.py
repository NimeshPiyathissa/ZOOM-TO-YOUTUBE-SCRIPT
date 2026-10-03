"""Scheduled join/go-live/stop (Asia/Colombo), an auto-recovery watchdog
for the case systemd itself gives up (unit reaches 'failed' after its
own restart-limit is exhausted), and an optional webhook alert.

Two independent scheduling mechanisms share this module:
- The Zoom page's per-source "join at" field (`_run_join_at`/the `join_at`
  block in `load_schedules`) - a one-off pre-join timestamp stored on a
  single source, unrelated to the richer schedules below.
- The Live Studio "Schedule" card / `/schedule` page's start/end-time
  schedules (the `schedules` table) - join early, go live exactly on
  time, stop + leave at the end, with retry/backoff on join failure and
  manual-override support. This is the newer, richer mechanism; see the
  "rich schedules" section below.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import subprocess
import time
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from . import config, db
from . import sources as sources_mod
from . import control

scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))
TZ = ZoneInfo(config.SCHEDULE_TIMEZONE)

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
    # unit_show()/read_current_source()/zoom_meeting_status() below all
    # shell out (sudo -u zoombot ...) - real fork/exec/PAM/D-Bus latency.
    # This watchdog runs as a recurring job on the same asyncio event loop
    # that serves /vnc/ws (app/vnc_proxy.py); calling them synchronously
    # here is exactly the "Reconnecting -> disconnected" bug (see
    # app/main.py's api_state fix) via a different call site - off the
    # loop via run_in_executor instead.
    loop = asyncio.get_event_loop()
    try:
        show = await loop.run_in_executor(None, control.unit_show, "ffmpeg-stream")
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
            cur_source = await loop.run_in_executor(None, control.read_current_source)
            if cur_source.get("SOURCE_TYPE") == "zoom":
                z_status = await loop.run_in_executor(None, control.zoom_meeting_status)
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


def _load_join_at_jobs() -> None:
    from datetime import datetime
    for s in sources_mod.list_sources():
        join_at = (s.get("options") or {}).get("join_at") if s["type"] == "zoom" else None
        if not join_at:
            continue
        when = datetime.fromtimestamp(float(join_at), ZoneInfo(config.TIMEZONE))
        if when.timestamp() < time.time() - 300:
            continue  # stale (dashboard was down when it was due) - shown as overdue on the page, never auto-fired late
        scheduler.add_job(_run_join_at, DateTrigger(run_date=when), args=[s["id"]],
                          id=f"join-at-{s['id']}", replace_existing=True)


def load_schedules() -> None:
    for job in list(scheduler.get_jobs()):
        if job.id not in ("watchdog", "verify-accounts", "youtube-watch", "rich-schedule-tick"):
            scheduler.remove_job(job.id)
    _load_join_at_jobs()
    _refresh_all_precise_jobs()


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


# ================================================================== rich schedules
# Live Studio "Schedule" card / /schedule page: start/end time, repeat
# (once/daily/weekdays/custom), join-lead, keep-meeting-open. One row in
# the `schedules` table per schedule (see app/db.py's migrations for the
# column set). Design: a 15s reconciliation tick is the source of truth
# and the sole thing that survives a crash/restart correctly (every tick
# recomputes "what should be happening right now" from the DB and nudges
# reality toward it); precise one-shot triggers are layered on top purely
# so go-live/stop land on the exact second instead of up to 15s late -
# they just call the same reconciliation function early.

TERMINAL_STATES = {"done", "skipped", "failed", "manually_stopped"}
NONTERMINAL_ZOOM_STATUSES = {"waiting_room", "connecting", "not_started", "unknown"}
JOIN_BACKOFF_SECONDS = [15, 30, 60, 120]
RICH_TICK_INTERVAL_SECONDS = 15

# Per-schedule-id bookkeeping that doesn't belong in the DB (purely
# operational pacing/overrides, not user-facing state):
_attempt_state: dict[int, dict] = {}   # {schedule_id: {"count": int, "last": datetime}}
_forced_runs: dict[int, dict] = {}     # {schedule_id: {"key": str, "end_at": datetime}}
_ntp_synced_cache: bool | None = None


def ntp_synced() -> bool | None:
    """Cached, refreshed by the rich-schedule tick - never call
    timedatectl synchronously from a request handler (see app/main.py's
    api_state fix for exactly why that's the wrong pattern here)."""
    return _ntp_synced_cache


def _refresh_ntp_cache() -> None:
    global _ntp_synced_cache
    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, timeout=5, text=True,
        )
        _ntp_synced_cache = proc.stdout.strip() == "yes"
    except Exception:
        _ntp_synced_cache = None


def _now() -> dt.datetime:
    return dt.datetime.now(TZ)


def now() -> dt.datetime:
    """Public: the current Asia/Colombo instant, for callers outside this
    module (e.g. app/main.py's schedule validation)."""
    return _now()


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def _active_weekdays(schedule: dict) -> set[int]:
    mode = schedule.get("repeat_mode") or "once"
    if mode == "daily":
        return set(range(7))
    if mode == "weekdays":
        return {0, 1, 2, 3, 4}
    if mode == "custom":
        raw = schedule.get("custom_days") or ""
        out = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 6:
                out.add(int(part))
        return out
    if mode == "once":
        d = _parse_date(schedule.get("start_date"))
        return {d.weekday()} if d else set()
    return set()


def occurrence_window(schedule: dict, date: dt.date) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    """(join_at, start_at, end_at) as Asia/Colombo-aware datetimes for the
    occurrence whose start falls on `date`."""
    start_at = dt.datetime.combine(date, dt.time(int(schedule["start_hour"]), int(schedule["start_minute"])), tzinfo=TZ)
    end_date = date + dt.timedelta(days=1) if schedule.get("overnight") else date
    end_at = dt.datetime.combine(end_date, dt.time(int(schedule["end_hour"]), int(schedule["end_minute"])), tzinfo=TZ)
    join_at = start_at - dt.timedelta(minutes=int(schedule.get("join_lead_minutes") or 0))
    return join_at, start_at, end_at


def find_current_or_next_occurrence(schedule: dict, now: dt.datetime):
    """The occurrence that's either currently active (join_at <= now <
    end_at) or, if none is, the next future one. Searches day-by-day
    starting the day before `now` (so an overnight/lead-time window that
    began "yesterday" by the calendar is still found) up to 9 days out.
    Returns (date, join_at, start_at, end_at) or None if this schedule has
    no valid active day at all."""
    days = _active_weekdays(schedule)
    if not days:
        return None
    start_date = _parse_date(schedule.get("start_date"))
    once = (schedule.get("repeat_mode") or "once") == "once"
    candidate = now.date() - dt.timedelta(days=1)
    for _ in range(9):
        valid_day = candidate.weekday() in days
        valid_date = start_date is None or candidate >= start_date
        valid_once = (not once) or (start_date is not None and candidate == start_date)
        if valid_day and valid_date and valid_once:
            join_at, start_at, end_at = occurrence_window(schedule, candidate)
            if end_at > now:
                return candidate, join_at, start_at, end_at
        candidate += dt.timedelta(days=1)
    return None


def active_days_for_response(schedule: dict) -> list[int]:
    return sorted(_active_weekdays(schedule))


def schedules_overlap(a: dict, b: dict) -> bool:
    """True if two (enabled) schedules could ever be live at the same
    time: they share at least one active weekday AND their [join_at,
    end_at) clock-time ranges (which can cross midnight) intersect.
    Checked globally, not per-source - only one stream pipeline exists."""
    days_a, days_b = _active_weekdays(a), _active_weekdays(b)
    if not (days_a & days_b):
        return False
    # Compare on a fixed reference date so clock-time ranges (which may
    # span midnight for an overnight run or an early join-lead) line up.
    ref = dt.date(2000, 1, 1)
    ja, sa, ea = occurrence_window(a, ref)
    jb, sb, eb = occurrence_window(b, ref)
    # Normalize relative to `ref`'s midnight as minute offsets so a range
    # that crosses into day+1 compares correctly against one that doesn't.
    def _mins(x: dt.datetime) -> int:
        return int((x - dt.datetime.combine(ref, dt.time(0, 0), tzinfo=TZ)).total_seconds() // 60)
    a_start, a_end = _mins(ja), _mins(ea)
    b_start, b_end = _mins(jb), _mins(eb)
    return a_start < b_end and b_start < a_end


def _persist_state(schedule_id: int, state: str, occurrence_date: str | None, detail: str | None) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE schedules SET state=?, state_occurrence_date=?, state_detail=?, last_run_at=?, updated_at=? WHERE id=?",
            (state, occurrence_date, detail, time.time(), time.time(), schedule_id),
        )


def _clear_skip_next(schedule_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE schedules SET skip_next=0, updated_at=? WHERE id=?", (time.time(), schedule_id))


def _record_attempt(schedule_id: int, now: dt.datetime) -> None:
    rec = _attempt_state.setdefault(schedule_id, {"count": 0, "last": now})
    rec["count"] += 1
    rec["last"] = now


def _backoff_due(schedule_id: int, now: dt.datetime) -> bool:
    rec = _attempt_state.get(schedule_id)
    if not rec:
        return True
    idx = min(rec["count"] - 1, len(JOIN_BACKOFF_SECONDS) - 1)
    return (now - rec["last"]).total_seconds() >= JOIN_BACKOFF_SECONDS[idx]


def _stop_and_leave(schedule: dict, ever_joined: bool) -> None:
    if not ever_joined:
        # Nothing of THIS schedule's own was ever started - don't touch
        # ffmpeg-stream or the active meeting, which could belong to an
        # unrelated session the operator started by hand in the meantime.
        return
    try:
        control.unit_action("ffmpeg-stream", "stop")
    except control.ControlError:
        pass
    if not schedule.get("keep_meeting_open"):
        try:
            env = control.read_current_source()
            if env.get("SOURCE_TYPE") == "zoom":
                control.zoom_join("leave", None)
            db.audit(None, "scheduled_leave", f"schedule={schedule['id']}")
        except control.ControlError:
            pass


def _try_join(schedule: dict, key: str, now: dt.datetime) -> None:
    source = sources_mod.get_source(schedule["source_id"]) if schedule.get("source_id") else None
    if not source:
        _persist_state(schedule["id"], "failed", key, "The saved source for this schedule no longer exists")
        db.audit(None, "scheduled_join_failed", f"schedule={schedule['id']} reason=source_deleted")
        return
    _record_attempt(schedule["id"], now)

    if source["type"] == "zoom":
        missing = sources_mod.zoom_missing(source)
        if missing:
            _persist_state(schedule["id"], "failed", key, "Not joinable: " + "; ".join(missing))
            db.audit(None, "scheduled_join_failed", f"schedule={schedule['id']} reason=not_joinable")
            return
        try:
            control.apply_source_config(source)
            sources_mod.set_active_source_id(source["id"])
            control.zoom_join("join", source)
        except control.ControlError as exc:
            _persist_state(schedule["id"], "joining", key, f"Join command failed, retrying: {exc}")
            db.audit(None, "scheduled_join_retry", f"schedule={schedule['id']} error={exc}")
            return
        status = control.zoom_meeting_status()
        st = status.get("status")
        if st == "in_meeting":
            _persist_state(schedule["id"], "joined", key, None)
            db.audit(None, "scheduled_join", f"schedule={schedule['id']} occurrence={key} source={source['name']}")
        elif status.get("terminal") or st in ("passcode_required", "registration_required", "wrong_registrant", "removed", "expired", "duplicate_join"):
            _persist_state(schedule["id"], "failed", key, f"Join failed: {st}")
            db.audit(None, "scheduled_join_failed", f"schedule={schedule['id']} reason={st}")
            asyncio.ensure_future(_send_alert(
                f"Scheduled join failed for '{schedule.get('label') or source['name']}': {st} - never went live"))
        else:
            _persist_state(schedule["id"], "joining", key, f"Waiting to join: {st or 'connecting'}")
            db.audit(None, "scheduled_join_retry", f"schedule={schedule['id']} status={st}")
    else:
        already_switched = schedule.get("state") == "joining"
        try:
            if not already_switched:
                control.apply_source_config(source)
                sources_mod.set_active_source_id(source["id"])
                unit = control.producer_unit_for(source)
                if unit:
                    control.unit_action("xvfb", "start")
                    control.unit_action("openbox", "start")
                    control.unit_action("audio-setup", "start")
                    control.unit_action(unit, "restart")
                    control.unit_action("x11vnc", "start")
            if source["type"] == "webpage":
                from . import stats
                if not stats.webpage_health().get("page_loaded"):
                    _persist_state(schedule["id"], "joining", key, "Waiting for the page to finish loading")
                    db.audit(None, "scheduled_join_retry", f"schedule={schedule['id']} status=page_loading")
                    return
            _persist_state(schedule["id"], "joined", key, None)
            db.audit(None, "scheduled_join", f"schedule={schedule['id']} occurrence={key} source={source['name']}")
        except control.ControlError as exc:
            _persist_state(schedule["id"], "joining", key, f"Switch failed, retrying: {exc}")
            db.audit(None, "scheduled_join_retry", f"schedule={schedule['id']} error={exc}")


def _tick_schedule(schedule: dict, now: dt.datetime) -> None:
    forced = _forced_runs.get(schedule["id"])
    if forced and schedule.get("state_occurrence_date") == forced["key"] and schedule.get("state") not in TERMINAL_STATES:
        occ = (now.date(), now, now, forced["end_at"])
    else:
        if forced:
            _forced_runs.pop(schedule["id"], None)
        occ = find_current_or_next_occurrence(schedule, now)
    if occ is None:
        return
    occurrence_date, join_at, start_at, end_at = occ
    key = forced["key"] if (forced and schedule.get("state_occurrence_date") == forced.get("key")) else occurrence_date.isoformat()

    if schedule.get("state_occurrence_date") != key:
        _attempt_state.pop(schedule["id"], None)
        if schedule.get("skip_next") and now < start_at:
            _persist_state(schedule["id"], "skipped", key, "Skipped by operator (Skip next)")
            _clear_skip_next(schedule["id"])
            db.audit(None, "scheduled_skip", f"schedule={schedule['id']} occurrence={key}")
            return
        _persist_state(schedule["id"], "idle", key, None)
        schedule = dict(schedule)
        schedule["state"] = "idle"
        schedule["state_occurrence_date"] = key

    state = schedule.get("state")
    if state in TERMINAL_STATES:
        return

    if now >= end_at:
        ever_joined = state in ("live", "joined")
        _stop_and_leave(schedule, ever_joined)
        reason = "Window ended" if ever_joined else "Window ended without ever joining"
        _persist_state(schedule["id"], "done", key, reason)
        db.audit(None, "scheduled_stop", f"schedule={schedule['id']} occurrence={key} reason={reason}")
        if (schedule.get("repeat_mode") or "once") == "once":
            with db.get_conn() as conn:
                conn.execute("UPDATE schedules SET enabled=0, updated_at=? WHERE id=?", (time.time(), schedule["id"]))
        return

    if now < join_at:
        return

    if state == "idle":
        _try_join(schedule, key, now)
    elif state == "joining":
        if _backoff_due(schedule["id"], now):
            _try_join(schedule, key, now)
    elif state == "joined" and now >= start_at:
        try:
            control.unit_action("ffmpeg-stream", "start")
            _persist_state(schedule["id"], "live", key, None)
            db.audit(None, "scheduled_go_live", f"schedule={schedule['id']} occurrence={key}")
        except control.ControlError as exc:
            _persist_state(schedule["id"], "joined", key, f"Go-live failed, will retry next tick: {exc}")


def _list_enabled_rich_schedules() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM schedules WHERE enabled=1 AND source_id IS NOT NULL "
                            "AND start_hour IS NOT NULL AND end_hour IS NOT NULL").fetchall()
    return [dict(r) for r in rows]


async def _rich_schedule_tick() -> None:
    # _refresh_ntp_cache() shells out (timedatectl) - same event-loop-
    # blocking hazard as _watchdog()'s unit_show() above, off the loop
    # for the same reason.
    await asyncio.get_event_loop().run_in_executor(None, _refresh_ntp_cache)
    now = _now()
    for schedule in _list_enabled_rich_schedules():
        try:
            await asyncio.get_event_loop().run_in_executor(None, _tick_schedule, schedule, now)
        except Exception as exc:  # noqa: BLE001 - one bad schedule must never kill the loop
            db.audit(None, "scheduled_tick_error", f"schedule={schedule.get('id')} error={exc}")
    _refresh_all_precise_jobs()


def _refresh_all_precise_jobs() -> None:
    """Precise one-shot triggers, purely so go-live/stop land on the exact
    second instead of up to RICH_TICK_INTERVAL_SECONDS late - they just
    call the same `_tick_schedule` reconciliation function early. The 15s
    tick above is what actually guarantees correctness/recovery; this is
    an optimization layered on top of it, not a second source of truth."""
    now = _now()
    seen_ids = set()
    for schedule in _list_enabled_rich_schedules():
        seen_ids.add(schedule["id"])
        forced = _forced_runs.get(schedule["id"])
        if forced and schedule.get("state_occurrence_date") == forced.get("key") and schedule.get("state") not in TERMINAL_STATES:
            moments = {"stop": forced["end_at"]}
        else:
            occ = find_current_or_next_occurrence(schedule, now)
            if occ is None:
                continue
            _, join_at, start_at, end_at = occ
            moments = {"join": join_at, "golive": start_at, "stop": end_at}
        for slot, when in moments.items():
            job_id = f"schedrun-{schedule['id']}-{slot}"
            if when <= now:
                continue
            scheduler.add_job(_fire_precise, DateTrigger(run_date=when), args=[schedule["id"]],
                              id=job_id, replace_existing=True, misfire_grace_time=60)
    # Drop precise jobs for schedules that are no longer enabled/complete.
    # Prefix is "schedrun-" (not "rich-") specifically so it can never
    # collide with this module's own "rich-schedule-tick" interval job.
    for job in scheduler.get_jobs():
        if job.id.startswith("schedrun-"):
            sched_id = int(job.id.split("-")[1])
            if sched_id not in seen_ids:
                scheduler.remove_job(job.id)


async def _fire_precise(schedule_id: int) -> None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id=? AND enabled=1", (schedule_id,)).fetchone()
    if not row:
        return
    await asyncio.get_event_loop().run_in_executor(None, _tick_schedule, dict(row), _now())


def note_manual_override() -> None:
    """The operator stopped the stream by hand (POST /api/stream/stop or
    /restart, or 'leave meeting' with then=stop) during a schedule's own
    window - manual control always wins, so that schedule's remaining
    reconciliation for THIS occurrence is suppressed (no rejoin/re-go-live)
    while the next occurrence still runs normally."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM schedules WHERE enabled=1 AND state IN ('joining','joined','live')"
        ).fetchall()
        ids = [r["id"] for r in rows]
        for sid in ids:
            conn.execute(
                "UPDATE schedules SET state='manually_stopped', "
                "state_detail='Stopped manually during the scheduled window', updated_at=? WHERE id=?",
                (time.time(), sid),
            )
    for sid in ids:
        db.audit(None, "scheduled_manual_override", f"schedule={sid}")


def run_now(schedule_id: int) -> dict:
    """Join + go live immediately, mapping this schedule's configured
    clock times onto today (or tomorrow's end time if overnight/already
    past), bypassing its day-pattern and join-lead gating. The normal
    day-pattern schedule still runs at its next real occurrence afterward."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if not row:
        raise ValueError("schedule not found")
    schedule = dict(row)
    now = _now()
    end_at = dt.datetime.combine(now.date(), dt.time(int(schedule["end_hour"]), int(schedule["end_minute"])), tzinfo=TZ)
    if schedule.get("overnight") or end_at <= now:
        end_at += dt.timedelta(days=1)
    key = f"manual-{now.strftime('%Y%m%dT%H%M%S')}"
    _forced_runs[schedule_id] = {"key": key, "end_at": end_at}
    _attempt_state.pop(schedule_id, None)
    _persist_state(schedule_id, "idle", key, "Started manually (Run now)")
    db.audit(None, "schedule_run_now", f"schedule={schedule_id}")
    return {"ok": True, "end_at": end_at.isoformat()}


def set_skip_next(schedule_id: int, value: bool) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE schedules SET skip_next=?, updated_at=? WHERE id=?", (1 if value else 0, time.time(), schedule_id))


def set_enabled(schedule_id: int, value: bool) -> None:
    with db.get_conn() as conn:
        if value:
            conn.execute(
                "UPDATE schedules SET enabled=1, state='idle', state_occurrence_date=NULL, "
                "state_detail=NULL, updated_at=? WHERE id=?",
                (time.time(), schedule_id),
            )
        else:
            conn.execute("UPDATE schedules SET enabled=0, updated_at=? WHERE id=?", (time.time(), schedule_id))
    _attempt_state.pop(schedule_id, None)
    _forced_runs.pop(schedule_id, None)


def next_run_info(schedule: dict) -> dict:
    """Computed, display-only fields for the UI: next_run (ISO, Asia/
    Colombo), countdown_seconds, active_days."""
    if not schedule.get("enabled") or not schedule.get("source_id") or schedule.get("start_hour") is None:
        return {"next_run": None, "countdown_seconds": None, "active_days": active_days_for_response(schedule)}
    now = _now()
    forced = _forced_runs.get(schedule["id"])
    if forced and schedule.get("state_occurrence_date") == forced.get("key") and schedule.get("state") not in TERMINAL_STATES:
        return {"next_run": now.isoformat(), "countdown_seconds": 0, "active_days": active_days_for_response(schedule)}
    occ = find_current_or_next_occurrence(schedule, now)
    if occ is None:
        return {"next_run": None, "countdown_seconds": None, "active_days": active_days_for_response(schedule)}
    _, join_at, start_at, _end_at = occ
    target = start_at if now < start_at else now  # already inside the window - "next run" is "now"
    return {
        "next_run": start_at.isoformat(),
        "join_at": join_at.isoformat(),
        "countdown_seconds": max(0, int((start_at - now).total_seconds())),
        "active_days": active_days_for_response(schedule),
    }


def start() -> None:
    from datetime import datetime, timedelta
    scheduler.add_job(_watchdog, "interval", seconds=WATCHDOG_INTERVAL_SECONDS, id="watchdog")
    scheduler.add_job(_verify_accounts, "interval", hours=ACCOUNT_VERIFY_INTERVAL_HOURS, id="verify-accounts",
                      next_run_time=datetime.now(ZoneInfo(config.TIMEZONE)) + timedelta(minutes=3))
    scheduler.add_job(_youtube_watch, "interval", seconds=YT_WATCH_INTERVAL_SECONDS, id="youtube-watch",
                      max_instances=1, coalesce=True)
    scheduler.add_job(_rich_schedule_tick, "interval", seconds=RICH_TICK_INTERVAL_SECONDS, id="rich-schedule-tick",
                      max_instances=1, coalesce=True)
    _refresh_ntp_cache()
    load_schedules()
    scheduler.start()
