"""Comprehensive unit test suite for:
- Recording Engine Start/Stop/Status
- VPS Local Storage Cleanup (/api/recordings/clean)
- Telegram Suite & Settings Store (/settings, /api/settings/telegram)
- Telethon 4GB Premium / Bot Uploader & Connection Testing
- Overview & Studio UI Recording Controls
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from app.main import app
from app import db, config, settings_store, recording, telegram, env_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_dashboard.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    db.init_db()

    # Isolated settings and recordings directory for tests
    test_settings_file = tmp_path / "settings.json"
    test_rec_dir = tmp_path / "recordings"
    test_rec_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SETTINGS_FILE", test_settings_file)
    monkeypatch.setattr(config, "RECORDINGS_DIR", test_rec_dir)

    # Mock env_store for cross-platform test reliability
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "test-key-1234", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
    monkeypatch.setattr(env_store, "masked_view", lambda: {"YT_STREAM_KEY": "••••1234"})

    from app import security
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("admin", security.hash_password("correct-horse-battery-staple"), time.time()),
        )
        user_id = cur.lastrowid
    session_id, csrf_token = security.create_session(user_id, "127.0.0.1")

    c = TestClient(app, cookies={config.COOKIE_NAME: session_id})
    c.session = {"id": session_id, "csrf_token": csrf_token, "username": "admin"}
    return c


# ==============================================================================
# 1. Settings Store Tests
# ==============================================================================

def test_settings_store_defaults_and_save(tmp_path, monkeypatch):
    test_file = tmp_path / "sub" / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", test_file)

    s = settings_store.load_settings()
    assert s["telegram_auto_upload_recording"] is True
    assert s["telegram_delete_after_upload"] is False
    assert s["telegram_notify_stream_events"] is True
    assert s["telegram_notify_system_errors"] is True
    assert s["telegram_bot_token"] == ""

    # Save updates
    updated = settings_store.save_settings({
        "telegram_bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        "telegram_chat_id": "-100987654321",
        "telegram_delete_after_upload": True,
    })
    assert updated["telegram_bot_token"] == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert updated["telegram_chat_id"] == "-100987654321"
    assert updated["telegram_delete_after_upload"] is True

    # Reload from disk
    reloaded = settings_store.load_settings()
    assert reloaded["telegram_bot_token"] == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert reloaded["telegram_chat_id"] == "-100987654321"
    assert reloaded["telegram_delete_after_upload"] is True

    # Masked view
    masked = settings_store.masked_settings()
    assert masked["telegram_bot_token"].startswith("••••••••")
    assert masked["has_bot_token"] is True
    assert masked["telegram_chat_id"] == "-100987654321"

    # Masked placeholder submitted does not overwrite existing secret
    settings_store.save_settings({"telegram_bot_token": "••••••••ew11"})
    kept = settings_store.load_settings()
    assert kept["telegram_bot_token"] == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


# ==============================================================================
# 2. Recording Engine & Storage Cleaning Tests
# ==============================================================================

def test_clean_recordings_preserves_active(tmp_path, monkeypatch):
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "RECORDINGS_DIR", rec_dir)

    old_rec = rec_dir / "rec_20260901_100000.mp4"
    old_rec.write_bytes(b"A" * 1024 * 1024)  # 1 MB
    # Make old file mtime 48 hours ago
    past_ts = time.time() - (48 * 3600)
    os.utime(str(old_rec), (past_ts, past_ts))

    active_rec = rec_dir / "rec_20260922_120000.mp4"
    active_rec.write_bytes(b"B" * 2 * 1024 * 1024)  # 2 MB

    # Mock recording status: active_rec is currently recording
    monkeypatch.setattr(recording, "get_status", lambda: {
        "recording": True,
        "file": str(active_rec),
        "duration": 50,
        "size_mb": 2.0,
        "free_gb": 15.0,
        "halted_reason": "",
    })

    # Run clean with default max_age_hours=24
    res = recording.clean_recordings(max_age_hours=24.0, force=False)
    assert res["ok"] is True
    assert res["deleted_count"] == 1
    assert res["freed_mb"] == 1.0
    assert not old_rec.exists()
    assert active_rec.exists()  # Active recording protected!

    # Run clean with force=True - active_rec must STILL be protected
    res_force = recording.clean_recordings(force=True)
    assert res_force["ok"] is True
    assert res_force["deleted_count"] == 0
    assert active_rec.exists()


def test_clean_recordings_force_purge(tmp_path, monkeypatch):
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "RECORDINGS_DIR", rec_dir)

    f1 = rec_dir / "rec_20260922_100000.mp4"
    f2 = rec_dir / "rec_20260922_110000.mp4"
    f1.write_bytes(b"X" * 1024 * 512)
    f2.write_bytes(b"Y" * 1024 * 512)

    monkeypatch.setattr(recording, "get_status", lambda: {"recording": False, "file": ""})

    res = recording.clean_recordings(force=True)
    assert res["ok"] is True
    assert res["deleted_count"] == 2
    assert not f1.exists()
    assert not f2.exists()


# ==============================================================================
# 3. Telegram Alerts & Telethon Uploader Tests
# ==============================================================================

def test_telegram_alert_toggles(monkeypatch):
    sent_alerts = []
    monkeypatch.setattr(telegram, "send_alert", lambda msg: sent_alerts.append(msg) or True)

    # When toggles are enabled (default)
    monkeypatch.setattr(settings_store, "get_setting", lambda k, d=True: True)
    assert telegram.alert_stream_started("Test Feed") is True
    assert len(sent_alerts) == 1
    assert "STREAM ON-AIR" in sent_alerts[-1]

    assert telegram.alert_stream_stopped("Test Feed", duration_sec=120) is True
    assert len(sent_alerts) == 2
    assert "STREAM STOPPED" in sent_alerts[-1]

    # When toggles are disabled
    monkeypatch.setattr(settings_store, "get_setting", lambda k, d=True: False)
    assert telegram.alert_stream_started("Test Feed") is False
    assert telegram.alert_stream_stopped("Test Feed") is False
    assert len(sent_alerts) == 2  # No new alerts sent


def test_telegram_upload_recording_auto_delete(tmp_path, monkeypatch):
    import asyncio
    test_video = tmp_path / "rec_20260922_140000.mp4"
    test_video.write_bytes(b"VIDEO_DATA_FOR_TESTING")

    monkeypatch.setattr(settings_store, "load_settings", lambda: {
        "telegram_auto_upload_recording": True,
        "telegram_delete_after_upload": True,
        "telegram_chat_id": "-1001234567890",
        "telegram_bot_token": "mock-token",
        "telegram_api_id": "12345",
        "telegram_api_hash": "mockhash",
        "telegram_session_string": "",
    })

    # Mock Telethon client inside telegram.py
    class MockTelethonClient:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self, bot_token):
            pass
        async def send_file(self, entity, file, **kwargs):
            assert file == str(test_video)
            assert "New Recording Ready" in kwargs.get("caption", "")
            assert "Local VPS copy purged" in kwargs.get("caption", "")
        async def disconnect(self):
            pass

    import telethon
    monkeypatch.setattr(telethon, "TelegramClient", MockTelethonClient)

    ok = asyncio.run(telegram.upload_recording_to_telegram(str(test_video), duration_sec=60, size_mb=1.5))
    assert ok is True
    # Local file must have been deleted to save storage!
    assert not test_video.exists()


def test_telegram_connection_test(monkeypatch):
    import asyncio
    # Test with no chat id
    res_err = asyncio.run(telegram.test_telegram_connection(bot_token="tok", chat_id=""))
    assert res_err["ok"] is False
    assert "Chat ID is required" in res_err["error"]

    # Test with mocked bot ping
    class MockResp:
        status_code = 200
        headers = {}
    class MockAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):  # noqa: F811 - matches httpx.AsyncClient.post's real kwarg name
            return MockResp()

    monkeypatch.setattr(telegram.httpx, "AsyncClient", MockAsyncClient)

    res_ok = asyncio.run(telegram.test_telegram_connection(bot_token="tok", chat_id="-1009999999"))
    assert res_ok["ok"] is True
    assert "delivered" in res_ok["message"]


# ==============================================================================
# 4. HTTP API Endpoints Tests
# ==============================================================================

def test_config_page_renders_telegram_suite(client):
    res = client.get("/config")
    assert res.status_code == 200
    html = res.text
    assert "Telegram Suite &amp; Cloud Storage" in html
    assert "cfg-tg-bot-token" in html
    assert "cfg-tg-chat-id" in html
    assert "cfg-tg-api-id" in html
    assert "cfg-tg-api-hash" in html
    assert "cfg-tg-session-string" in html
    assert "btn-test-telegram" in html
    assert "btn-clean-recordings" in html


def test_settings_alias_page_renders_telegram_suite(client):
    res = client.get("/settings")
    assert res.status_code == 200
    assert "Telegram Suite &amp; Cloud Storage" in res.text


def test_overview_page_renders_recording_controls(client):
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert "btn-overview-record" in html
    assert "overview-rec-pill" in html
    assert "Start Recording" in html


def test_studio_page_renders_recording_controls(client):
    res = client.get("/studio")
    assert res.status_code == 200
    html = res.text
    assert "btn-studio-record" in html
    assert "studio-rec-pill" in html
    assert "/home/dashboard/recordings/" in html


def test_api_record_endpoints(client, monkeypatch):
    monkeypatch.setattr(recording, "get_status", lambda: {"recording": False, "file": "", "duration": 0, "size_mb": 0.0, "free_gb": 20.0, "halted_reason": ""})
    monkeypatch.setattr(recording, "record_start", lambda: {"recording": True, "file": "/home/dashboard/recordings/rec_test.mp4", "duration": 0, "size_mb": 0.0, "free_gb": 20.0, "halted_reason": ""})
    monkeypatch.setattr(recording, "record_stop", lambda: {"recording": False, "file": "/home/dashboard/recordings/rec_test.mp4", "duration": 45, "size_mb": 5.2, "free_gb": 20.0, "halted_reason": ""})

    # GET status
    get_res = client.get("/api/record")
    assert get_res.status_code == 200
    assert get_res.json()["recording"] is False

    # POST start
    start_res = client.post("/api/record", json={"action": "start"}, headers={"X-CSRF-Token": client.session["csrf_token"]})
    assert start_res.status_code == 200
    assert start_res.json()["recording"] is True

    # POST stop
    stop_res = client.post("/api/record", json={"action": "stop"}, headers={"X-CSRF-Token": client.session["csrf_token"]})
    assert stop_res.status_code == 200
    assert stop_res.json()["recording"] is False


def test_api_recordings_clean_endpoint(client, monkeypatch):
    monkeypatch.setattr(recording, "clean_recordings", lambda max_age_hours=24.0, force=False: {
        "ok": True,
        "deleted_count": 3,
        "freed_mb": 150.5,
        "freed_gb": 0.15,
        "free_gb": 25.4,
        "message": "Purged 3 recording(s)",
    })

    res = client.post("/api/recordings/clean", json={"force": True}, headers={"X-CSRF-Token": client.session["csrf_token"]})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["deleted_count"] == 3
    assert data["freed_mb"] == 150.5


def test_api_settings_telegram_endpoints(client):
    # GET settings
    get_res = client.get("/api/settings/telegram")
    assert get_res.status_code == 200
    assert "telegram_bot_token" in get_res.json()
    assert "has_bot_token" in get_res.json()

    # POST settings
    post_res = client.post("/api/settings/telegram", json={
        "telegram_bot_token": "987654:XYZ-TOKEN-TEST",
        "telegram_chat_id": "-100555555",
        "telegram_auto_upload_recording": False,
    }, headers={"X-CSRF-Token": client.session["csrf_token"]})
    assert post_res.status_code == 200
    assert post_res.json()["ok"] is True
    assert post_res.json()["settings"]["telegram_chat_id"] == "-100555555"
    assert post_res.json()["settings"]["telegram_auto_upload_recording"] is False


def test_api_telegram_test_endpoint(client, monkeypatch):
    async def mock_test(*args, **kwargs):
        return {"ok": True, "message": "Test passed"}
    monkeypatch.setattr(telegram, "test_telegram_connection", mock_test)

    res = client.post("/api/telegram/test", json={"telegram_chat_id": "-100123"}, headers={"X-CSRF-Token": client.session["csrf_token"]})
    assert res.status_code == 200
    assert res.json()["ok"] is True
