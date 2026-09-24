"""Persistent settings manager for Telegram suite and cloud storage configuration.
Stores configuration in /home/dashboard/app/settings.json.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
from typing import Any

from . import config

logger = logging.getLogger("zoom-stream.settings")

DEFAULT_SETTINGS: dict[str, Any] = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_api_id": "",
    "telegram_api_hash": "",
    "telegram_session_string": "",
    "telegram_auto_upload_recording": True,
    "telegram_delete_after_upload": False,
    "telegram_notify_stream_events": True,
    "telegram_notify_system_errors": True,
    "vnc_password": "",
    "background_image": "/static/img/bg_nature.jpg",
}


def _get_settings_file() -> pathlib.Path:
    return config.SETTINGS_FILE


def load_settings() -> dict[str, Any]:
    """Reads settings from settings.json, merging with defaults and env fallbacks."""
    settings_file = _get_settings_file()
    settings: dict[str, Any] = dict(DEFAULT_SETTINGS)

    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULT_SETTINGS:
                        if isinstance(DEFAULT_SETTINGS[k], bool):
                            settings[k] = bool(v)
                        else:
                            settings[k] = str(v) if v is not None else ""
        except Exception as exc:
            logger.warning("Failed to read settings file %s: %s", settings_file, exc)
    else:
        # Fallback to .env values if available on initial start
        try:
            from . import env_store
            env = env_store.read_parsed()
            if env.get("TELEGRAM_BOT_TOKEN"):
                settings["telegram_bot_token"] = env["TELEGRAM_BOT_TOKEN"]
            if env.get("TELEGRAM_CHAT_ID"):
                settings["telegram_chat_id"] = env["TELEGRAM_CHAT_ID"]
            if env.get("VNC_PASSWORD"):
                settings["vnc_password"] = env["VNC_PASSWORD"]
        except Exception:
            pass

    if not settings.get("vnc_password"):
        try:
            from . import env_store
            env = env_store.read_parsed()
            if env.get("VNC_PASSWORD"):
                settings["vnc_password"] = env["VNC_PASSWORD"]
        except Exception:
            pass

    return settings


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Saves settings updates to settings.json atomically."""
    current = load_settings()
    for k, v in updates.items():
        if k in DEFAULT_SETTINGS:
            if isinstance(DEFAULT_SETTINGS[k], bool):
                current[k] = bool(v)
            else:
                # If a masked placeholder was submitted (e.g. "••••••••"), do not overwrite existing value
                val_str = str(v).strip() if v is not None else ""
                if val_str.startswith("••••") and current.get(k):
                    continue
                current[k] = val_str

    settings_file = _get_settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write
    dir_name = str(settings_file.parent)
    fd, tmp_path = tempfile.mkstemp(prefix="settings_", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp_path, str(settings_file))
        # Ensure proper permissions if run as dashboard
        try:
            os.chmod(str(settings_file), 0o664)
        except Exception:
            pass
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return current


def get_setting(key: str, default: Any = None) -> Any:
    """Returns a specific setting value."""
    settings = load_settings()
    return settings.get(key, default if default is not None else DEFAULT_SETTINGS.get(key))


def mask_secret(val: str, keep_last: int = 4) -> str:
    """Masks secret string for display in UI."""
    if not val:
        return ""
    val = val.strip()
    if len(val) <= keep_last:
        return "••••••••"
    return "••••••••" + val[-keep_last:]


def masked_settings() -> dict[str, Any]:
    """Returns a view of settings suitable for frontend display."""
    settings = load_settings()
    return {
        "telegram_bot_token": mask_secret(settings.get("telegram_bot_token", "")),
        "telegram_chat_id": settings.get("telegram_chat_id", ""),
        "telegram_api_id": settings.get("telegram_api_id", ""),
        "telegram_api_hash": mask_secret(settings.get("telegram_api_hash", "")),
        "telegram_session_string": mask_secret(settings.get("telegram_session_string", ""), keep_last=6),
        "telegram_auto_upload_recording": bool(settings.get("telegram_auto_upload_recording", True)),
        "telegram_delete_after_upload": bool(settings.get("telegram_delete_after_upload", False)),
        "telegram_notify_stream_events": bool(settings.get("telegram_notify_stream_events", True)),
        "telegram_notify_system_errors": bool(settings.get("telegram_notify_system_errors", True)),
        "has_bot_token": bool(settings.get("telegram_bot_token")),
        "has_api_id": bool(settings.get("telegram_api_id")),
        "has_api_hash": bool(settings.get("telegram_api_hash")),
        "has_session_string": bool(settings.get("telegram_session_string")),
    }
