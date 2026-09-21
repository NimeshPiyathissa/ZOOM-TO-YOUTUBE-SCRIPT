"""Telegram broadcast alert engine for live stream lifecycle and telemetry events.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env and sends real-time
HTML-formatted alerts.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import env_store

logger = logging.getLogger("zoom-stream.telegram")

# Cooldown tracking for repetitive alerts (e.g., dropped frames)
_last_dropped_frames_alert_ts = 0.0
DROPPED_FRAMES_COOLDOWN_SEC = 300.0  # 5 minutes cooldown


def get_telegram_config() -> tuple[str, str]:
    """Returns (bot_token, chat_id). Empty strings if not configured."""
    try:
        env = env_store.read_parsed()
    except Exception:
        env = {}
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


async def send_alert_async(message: str) -> bool:
    """Sends an HTML formatted alert message to the configured Telegram chat."""
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning("Telegram API responded with HTTP %d: %s", resp.status_code, resp.text[:200])
            return False
    except Exception as exc:
        logger.warning("Failed to send Telegram alert: %s", exc)
        return False


def send_alert(message: str) -> bool:
    """Synchronous alert sender safe to call from background threads or scripts."""
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Failed to send synchronous Telegram alert: %s", exc)
        return False


# --- Real-Time Trigger Handlers ---

def alert_stream_started(source_name: str = "Active Feed") -> bool:
    """Fired when live broadcast encoder starts (ON-AIR)."""
    msg = (
        "🔴 <b>STREAM ON-AIR</b>\n\n"
        f"<b>Source:</b> {source_name}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Broadcast stream encoder has started transmitting to YouTube."
    )
    return send_alert(msg)


def alert_stream_stopped(source_name: str = "Active Feed", duration_sec: int | None = None) -> bool:
    """Fired when live broadcast encoder stops (OFF-AIR)."""
    dur_str = f"{duration_sec // 60}m {duration_sec % 60}s" if duration_sec is not None else "N/A"
    msg = (
        "⏹️ <b>STREAM STOPPED</b>\n\n"
        f"<b>Source:</b> {source_name}\n"
        f"<b>Duration:</b> {dur_str}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Broadcast stream encoder is now offline."
    )
    return send_alert(msg)


def alert_high_dropped_frames(
    dropped_frames: int,
    total_frames: int,
    drop_pct: float,
    fps: float = 0.0,
    bitrate_kbps: float = 0.0,
    force: bool = False,
) -> bool:
    """Fired when frame drop rate exceeds 5%."""
    global _last_dropped_frames_alert_ts
    now = time.monotonic()
    if not force and (now - _last_dropped_frames_alert_ts < DROPPED_FRAMES_COOLDOWN_SEC):
        return False

    _last_dropped_frames_alert_ts = now
    msg = (
        "⚠️ <b>HIGH DROPPED FRAMES ALERT (&gt;5%)</b>\n\n"
        f"<b>Drop Rate:</b> {drop_pct:.1f}% ({dropped_frames} / {total_frames} frames)\n"
        f"<b>Current FPS:</b> {fps:.1f}\n"
        f"<b>Current Bitrate:</b> {bitrate_kbps:.0f} kbps\n\n"
        "Network connection or encoder CPU may be degraded. Stream output quality is impacted."
    )
    return send_alert(msg)


def alert_zoom_disconnected(detail: str = "Meeting disconnected or ended") -> bool:
    """Fired when Zoom disconnects or encounters a terminal state."""
    msg = (
        "⚠️ <b>ZOOM DISCONNECT DETECTED</b>\n\n"
        f"<b>Status:</b> {detail}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Zoom session is no longer active. Emergency BRB holding card can be engaged."
    )
    return send_alert(msg)
