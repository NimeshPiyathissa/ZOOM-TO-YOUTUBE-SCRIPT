"""Telegram broadcast alert engine & Telethon 4GB Premium uploader.
Reads configuration from /home/dashboard/app/settings.json (with .env fallback)
and handles stream lifecycle alerts, command polling, and background recording uploads.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

import httpx

from . import env_store, settings_store

logger = logging.getLogger("zoom-stream.telegram")

# Cooldown tracking for repetitive alerts (e.g., dropped frames)
_last_dropped_frames_alert_ts = 0.0
DROPPED_FRAMES_COOLDOWN_SEC = 300.0  # 5 minutes cooldown


def get_telegram_config() -> tuple[str, str]:
    """Returns (bot_token, chat_id). Empty strings if not configured."""
    try:
        settings = settings_store.load_settings()
        token = (settings.get("telegram_bot_token") or "").strip()
        chat_id = (settings.get("telegram_chat_id") or "").strip()
        if token and chat_id:
            return token, chat_id
    except Exception:
        pass

    try:
        env = env_store.read_parsed()
        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
        return token, chat_id
    except Exception:
        return "", ""


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
        async with httpx.AsyncClient(timeout=8.0) as client:
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
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Failed to send synchronous Telegram alert: %s", exc)
        return False


# --- Real-Time Trigger Handlers ---

def alert_stream_started(source_name: str = "Active Feed") -> bool:
    """Fired when live broadcast encoder starts (ON-AIR)."""
    if not settings_store.get_setting("telegram_notify_stream_events", True):
        return False
    msg = (
        "🔴 <b>STREAM ON-AIR</b>\n\n"
        f"<b>Source:</b> {source_name}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Broadcast stream encoder has started transmitting to YouTube."
    )
    return send_alert(msg)


def alert_stream_stopped(source_name: str = "Active Feed", duration_sec: int | None = None) -> bool:
    """Fired when live broadcast encoder stops (OFF-AIR)."""
    if not settings_store.get_setting("telegram_notify_stream_events", True):
        return False
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
    if not settings_store.get_setting("telegram_notify_system_errors", True):
        return False
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
    if not settings_store.get_setting("telegram_notify_system_errors", True):
        return False
    msg = (
        "⚠️ <b>ZOOM DISCONNECT DETECTED</b>\n\n"
        f"<b>Status:</b> {detail}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Zoom session is no longer active. Emergency BRB holding card can be engaged."
    )
    return send_alert(msg)


def alert_system_error(component: str, error_detail: str) -> bool:
    """Fired on encoder or service crashes."""
    if not settings_store.get_setting("telegram_notify_system_errors", True):
        return False
    msg = (
        f"🚨 <b>SYSTEM ERROR: {component.upper()}</b>\n\n"
        f"<b>Detail:</b> {error_detail}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Check Dashboard service logs for immediate diagnosis."
    )
    return send_alert(msg)


def alert_clean_feed_triggered(trigger_source: str = "Studio / Operator") -> bool:
    """Fired when Zoom clean-feed enforcement is triggered."""
    msg = (
        "🧹 <b>ZOOM CLEAN-FEED APPLIED</b>\n\n"
        f"<b>Trigger:</b> {trigger_source}\n"
        f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "Controls eliminated and full edge-to-edge canvas fit enforced."
    )
    return send_alert(msg)


# --- Telegram Connection Test ---

async def test_telegram_connection(
    bot_token: str = "",
    chat_id: str = "",
    api_id: str = "",
    api_hash: str = "",
    session_string: str = "",
) -> dict[str, Any]:
    """Tests connection to Telegram using provided or stored credentials."""
    settings = settings_store.load_settings()
    token = (bot_token or settings.get("telegram_bot_token") or "").strip()
    target_chat = (chat_id or settings.get("telegram_chat_id") or "").strip()
    a_id = (api_id or settings.get("telegram_api_id") or "").strip()
    a_hash = (api_hash or settings.get("telegram_api_hash") or "").strip()
    s_str = (session_string or settings.get("telegram_session_string") or "").strip()

    if not target_chat:
        return {"ok": False, "error": "Telegram Chat ID is required"}

    # Test 1: If session string + api_id + api_hash available, test Telethon client
    if s_str and a_id and a_hash:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            client = TelegramClient(StringSession(s_str), int(a_id), a_hash)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return {"ok": False, "error": "Telegram Premium session string is invalid or expired"}
            
            dest = int(target_chat) if (target_chat.startswith("-") and target_chat[1:].isdigit()) or target_chat.isdigit() else target_chat
            ts = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
            msg = f"🔔 **Telegram Premium Connection Test**\n\n✅ Successfully connected via Telethon User Session!\n⏱ Server Time: {ts}"
            await client.send_message(dest, msg)
            await client.disconnect()
            return {"ok": True, "message": f"Telethon Premium test message delivered to {target_chat}"}
        except Exception as exc:
            logger.warning("Telethon session connection test failed: %s", exc)
            return {"ok": False, "error": f"Telethon session error: {exc}"}

    # Test 2: Standard Bot API test
    if token:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        ts = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        payload = {
            "chat_id": target_chat,
            "text": (
                "🔔 <b>Telegram Bot Connection Test</b>\n\n"
                "✅ Successfully connected to Zoom Stream Dashboard!\n"
                f"⏱ Server Time: {ts}"
            ),
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return {"ok": True, "message": f"Telegram Bot test message delivered to {target_chat}"}
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                err = data.get("description", resp.text[:200])
                return {"ok": False, "error": f"Telegram API error ({resp.status_code}): {err}"}
        except Exception as exc:
            return {"ok": False, "error": f"Connection failed: {exc}"}

    return {"ok": False, "error": "Neither Bot Token nor Telegram Premium Session String is configured"}


# --- Telethon 4GB Background Uploader ---

async def upload_recording_to_telegram(
    file_path: str,
    duration_sec: int = 0,
    size_mb: float = 0.0,
) -> bool:
    """Uploads local MP4 file to Telegram via Telethon (up to 4GB with Premium session,
    2GB with bot token) or HTTP Bot API (<50MB).
    If telegram_delete_after_upload is True, purges local MP4 upon 100% upload completion.
    """
    settings = settings_store.load_settings()
    if not settings.get("telegram_auto_upload_recording", True):
        logger.info("Telegram auto-upload disabled in settings; skipping upload for %s", file_path)
        return False

    if not os.path.isfile(file_path):
        logger.error("Recording file not found for Telegram upload: %s", file_path)
        return False

    actual_size = os.path.getsize(file_path)
    if actual_size == 0:
        logger.warning("Recording file is empty (0 bytes); skipping upload: %s", file_path)
        return False

    calc_size_mb = round(actual_size / (1024 * 1024), 1)
    if size_mb <= 0:
        size_mb = calc_size_mb

    chat_id_str = (settings.get("telegram_chat_id") or "").strip()
    bot_token = (settings.get("telegram_bot_token") or "").strip()
    api_id_str = (settings.get("telegram_api_id") or "").strip()
    api_hash = (settings.get("telegram_api_hash") or "").strip()
    session_string = (settings.get("telegram_session_string") or "").strip()
    delete_after = bool(settings.get("telegram_delete_after_upload", False))

    if not chat_id_str:
        logger.warning("Telegram upload skipped: telegram_chat_id is not set.")
        return False

    # Format caption
    h = duration_sec // 3600
    m = (duration_sec % 3600) // 60
    s = duration_sec % 60
    dur_str = f"{h:02d}:{m:02d}:{s:02d}"

    mtime = os.path.getmtime(file_path)
    date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))

    caption = (
        "📹 *New Recording Ready*\n"
        f"🗓 Date: {date_str}\n"
        f"⏱ Duration: {dur_str}\n"
        f"📦 Size: {size_mb:.1f} MB"
    )
    if delete_after:
        caption += "\n🗑 Local VPS copy purged to save storage"

    target: int | str = (
        int(chat_id_str)
        if (chat_id_str.startswith("-") and chat_id_str[1:].isdigit()) or chat_id_str.isdigit()
        else chat_id_str
    )

    upload_ok = False

    # Strategy 1: Telethon Client (Handles up to 4GB with session_string, or 2GB with bot_token)
    if api_id_str and api_hash:
        try:
            api_id = int(api_id_str)
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            from telethon.tl.types import DocumentAttributeVideo

            client: TelegramClient | None = None
            if session_string:
                logger.info("Initializing Telethon with User Session String (up to 4GB Premium support)...")
                client = TelegramClient(StringSession(session_string), api_id, api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    logger.warning("Telethon session string not authorized, falling back to bot token if available")
                    await client.disconnect()
                    client = None

            if client is None and bot_token:
                logger.info("Initializing Telethon with Bot Token (up to 2GB support)...")
                client = TelegramClient(StringSession(""), api_id, api_hash)
                await client.start(bot_token=bot_token)

            if client:
                attrs = [
                    DocumentAttributeVideo(
                        duration=int(duration_sec),
                        w=1920,
                        h=1080,
                        supports_streaming=True,
                    )
                ]
                logger.info("Starting Telethon stream-upload for %s (%s MB) to %s", file_path, size_mb, target)
                await client.send_file(
                    entity=target,
                    file=file_path,
                    caption=caption,
                    parse_mode="md",
                    supports_streaming=True,
                    attributes=attrs,
                )
                await client.disconnect()
                upload_ok = True
                logger.info("Telethon upload completed successfully for %s", file_path)
        except Exception as exc:
            logger.error("Telethon upload failed: %s", exc)

    # Strategy 2: HTTP Bot API Fallback (for files <= 50MB)
    if not upload_ok and bot_token and actual_size <= 50 * 1024 * 1024:
        try:
            logger.info("Falling back to Telegram Bot HTTP API for %s (<= 50MB)...", file_path)
            url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
            with open(file_path, "rb") as vf:
                files = {"video": (os.path.basename(file_path), vf, "video/mp4")}
                data = {
                    "chat_id": chat_id_str,
                    "caption": caption.replace("*", "<b>").replace("*", "</b>"),
                    "parse_mode": "HTML",
                    "supports_streaming": "true",
                }
                async with httpx.AsyncClient(timeout=300.0) as client_http:
                    resp = await client_http.post(url, data=data, files=files)
                    if resp.status_code == 200:
                        upload_ok = True
                        logger.info("Bot HTTP API upload completed successfully for %s", file_path)
                    else:
                        logger.warning("Bot HTTP API returned %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("Bot HTTP API upload error: %s", exc)

    if not upload_ok:
        logger.error("Telegram upload could not complete for %s. Check API credentials.", file_path)
        return False

    # Cleanup local VPS storage if configured
    if delete_after:
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
                logger.info("Local recording purged after Telegram upload: %s", file_path)
        except Exception as exc:
            logger.error("Failed to delete local recording after upload: %s", exc)

    return True


def trigger_recording_upload(file_path: str, duration_sec: int = 0, size_mb: float = 0.0) -> None:
    """Spawns an asynchronous background worker to upload the MP4 file to Telegram."""
    def _run_bg():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(upload_recording_to_telegram(file_path, duration_sec, size_mb))
        except Exception as exc:
            logger.error("Background recording upload exception: %s", exc)
        finally:
            loop.close()

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(upload_recording_to_telegram(file_path, duration_sec, size_mb))
    except RuntimeError:
        t = threading.Thread(target=_run_bg, daemon=True, name="telethon-uploader")
        t.start()


# --- Telegram Command Poller ---

_telegram_poller_task: asyncio.Task | None = None


async def _poll_telegram_commands_loop():
    """Polls Telegram getUpdates for /clean and /cleanfeed commands."""
    last_update_id = 0
    while True:
        try:
            token, allowed_chat_id = get_telegram_config()
            if not token:
                await asyncio.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 5}
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        uid = update.get("update_id", 0)
                        if uid > last_update_id:
                            last_update_id = uid
                        msg = update.get("message") or update.get("channel_post") or {}
                        chat = msg.get("chat", {})
                        chat_id = str(chat.get("id", ""))
                        if allowed_chat_id and chat_id != allowed_chat_id:
                            continue
                        text = (msg.get("text") or "").strip().lower()
                        if text in ("/clean", "/cleanfeed", "/clean_feed", "clean", "clean feed"):
                            from . import zoom_web
                            res = await zoom_web.apply_cleanfeed_async()
                            if res.get("ok"):
                                reply = (
                                    "✅ <b>Clean feed applied!</b>\n"
                                    "Zoom Workplace top header, bottom toolbar, and black borders eliminated."
                                )
                            else:
                                reply = f"⚠️ <b>Clean feed trigger:</b> {res.get('error', 'CDP evaluated')}"
                            await send_alert_async(reply)
        except Exception as exc:
            logger.debug("Telegram polling exception: %s", exc)
        await asyncio.sleep(3)


def start_telegram_poller():
    global _telegram_poller_task
    if _telegram_poller_task is None or _telegram_poller_task.done():
        try:
            loop = asyncio.get_running_loop()
            _telegram_poller_task = loop.create_task(_poll_telegram_commands_loop())
        except RuntimeError:
            pass


def stop_telegram_poller():
    global _telegram_poller_task
    if _telegram_poller_task and not _telegram_poller_task.done():
        _telegram_poller_task.cancel()
        _telegram_poller_task = None
