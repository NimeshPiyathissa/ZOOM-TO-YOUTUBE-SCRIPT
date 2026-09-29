"""Recording engine management and storage maintenance.
Controls isolated FFmpeg recording to /home/dashboard/recordings/rec_YYYYMMDD_HHMMSS.mp4
and manages VPS storage cleanup and Telegram upload triggers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shutil
import time
from typing import Any

from . import config, settings_store

logger = logging.getLogger("zoom-stream.recording")


def get_recordings_dir() -> pathlib.Path:
    """Returns recordings directory, ensuring it exists."""
    p = config.RECORDINGS_DIR
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Could not create recordings dir %s: %s", p, exc)
    return p


def get_free_disk_gb(path: pathlib.Path | None = None) -> float:
    """Returns free disk space in GB."""
    p = path or get_recordings_dir()
    try:
        usage = shutil.disk_usage(str(p))
        return round(usage.free / (1024 * 1024 * 1024), 2)
    except Exception:
        return 0.0


def get_status() -> dict[str, Any]:
    """Retrieves current recording status."""
    try:
        from . import control
        res = control.record_stream_action("status")
        return res
    except Exception as exc:
        logger.debug("Error getting recording status: %s", exc)
        return {
            "recording": False,
            "file": "",
            "duration": 0,
            "size_mb": 0.0,
            "free_gb": get_free_disk_gb(),
            "halted_reason": "",
        }


def record_start() -> dict[str, Any]:
    """Starts local isolated stream recording."""
    from . import control
    res = control.record_stream_action("start")
    return res


def record_stop() -> dict[str, Any]:
    """Stops recording cleanly (SIGINT/SIGTERM with 2s grace period).
    If telegram_auto_upload_recording is active, schedules background upload.
    """
    from . import control
    res = control.record_stream_action("stop")
    stopped_file = res.get("file", "")

    # Trigger background upload if file exists and auto-upload enabled
    if stopped_file and os.path.isfile(stopped_file) and os.path.getsize(stopped_file) > 0:
        if settings_store.get_setting("telegram_auto_upload_recording", True):
            try:
                from . import telegram
                dur = int(res.get("duration") or 0)
                sz = float(res.get("size_mb") or 0.0)
                telegram.trigger_recording_upload(stopped_file, duration_sec=dur, size_mb=sz)
                res["upload_scheduled"] = True
            except Exception as exc:
                logger.error("Failed to trigger recording upload: %s", exc)
                res["upload_error"] = str(exc)

    return res


def clean_recordings(max_age_hours: float = 24.0, force: bool = False) -> dict[str, Any]:
    """Purges MP4 recordings on VPS to preserve disk space.
    If force=True, removes all completed recordings.
    If force=False, removes recordings older than max_age_hours (default 24h).
    Never deletes the actively recording file.
    """
    rec_dir = get_recordings_dir()
    if not rec_dir.exists():
        return {
            "ok": True,
            "deleted_count": 0,
            "freed_mb": 0.0,
            "freed_gb": 0.0,
            "free_gb": get_free_disk_gb(rec_dir),
            "message": "No recordings directory found",
        }

    # Determine active recording file to protect it
    status = get_status()
    active_file = os.path.abspath(status.get("file", "")) if status.get("recording") else ""

    deleted_count = 0
    freed_bytes = 0
    now = time.time()
    cutoff_ts = now - (max_age_hours * 3600.0)

    for item in rec_dir.glob("*.mp4"):
        if not item.is_file():
            continue
        item_path = str(item.resolve())
        if active_file and item_path == active_file:
            logger.debug("Skipping active recording file: %s", item_path)
            continue

        try:
            st = item.stat()
            if force or st.st_mtime < cutoff_ts:
                file_size = st.st_size
                item.unlink()
                deleted_count += 1
                freed_bytes += file_size
                logger.info("Cleaned local recording: %s (%d bytes)", item_path, file_size)
        except Exception as exc:
            logger.warning("Could not delete recording file %s: %s", item, exc)

    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    freed_gb = round(freed_bytes / (1024 * 1024 * 1024), 2)
    free_gb = get_free_disk_gb(rec_dir)

    if deleted_count > 0:
        msg = f"Purged {deleted_count} recording(s), freed {freed_mb:.1f} MB (Current free: {free_gb:.2f} GB)"
    else:
        msg = f"No eligible recordings to clean (Current free: {free_gb:.2f} GB)"

    return {
        "ok": True,
        "deleted_count": deleted_count,
        "freed_mb": freed_mb,
        "freed_gb": freed_gb,
        "free_gb": free_gb,
        "message": msg,
    }
