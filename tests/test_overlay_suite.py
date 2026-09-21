"""Comprehensive unit tests for:
- OBS-Style Text Overlay Studio (/overlay, /api/overlay, /api/overlay/toggle)
- Emergency Failover BRB Slate (/api/slate/brb)
- Telegram Broadcast Alerts (telegram.py)
- Local Stream Recording & Safety Halt (/api/record)
"""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from app.main import app
from app import config, db, overlay, slate, telegram, control, env_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_dashboard.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "OVERLAY_CONFIG_FILE", tmp_path / "data" / "overlay.json")
    monkeypatch.setattr(config, "BRB_SLATE_FILE", tmp_path / "data" / "brb_slate.json")
    db.init_db()

    from app import security
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("admin", security.hash_password("correct-horse-battery-staple"), time.time()),
        )
        user_id = cur.lastrowid
    session_id, csrf_token = security.create_session(user_id, "127.0.0.1")

    client = TestClient(app, cookies={config.COOKIE_NAME: session_id})
    client.session = {"id": session_id, "csrf_token": csrf_token, "username": "admin"}
    return client


# --- 1. Overlay Studio Tests ---

def test_overlay_page_authenticated(client):
    res = client.get("/overlay")
    assert res.status_code == 200
    html = res.text
    assert "OBS-Style Text Overlay Studio" in html
    assert "16:9 Live Broadcast Canvas" in html
    assert "SHOW OVERLAY" in html or "HIDE OVERLAY" in html
    assert "Google Font Family" in html
    assert "Text Outline / Stroke" in html
    assert "Background Holding Box" in html
    assert "Drop Shadow" in html


def test_overlay_api_get_and_save(client):
    res = client.get("/api/overlay")
    assert res.status_code == 200
    data = res.json()
    assert "text" in data
    assert "font_family" in data
    assert "pos_x" in data

    # Save updates
    updates = {
        "text": "BREAKING NEWS",
        "font_family": "Bebas Neue",
        "font_size": 54,
        "font_color": "#FF0000",
        "outline_enabled": True,
        "outline_color": "#FFFFFF",
        "pos_x": 10.0,
        "pos_y": 85.0,
        "visible": True,
    }
    res = client.post(
        "/api/overlay",
        json=updates,
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    saved = res.json()["state"]
    assert saved["text"] == "BREAKING NEWS"
    assert saved["font_family"] == "Bebas Neue"
    assert saved["font_size"] == 54
    assert saved["visible"] is True


def test_overlay_api_toggle(client):
    res = client.post(
        "/api/overlay/toggle",
        json={},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    data = res.json()
    first_state = data["state"]["visible"]

    res2 = client.post(
        "/api/overlay/toggle",
        json={},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res2.status_code == 200
    second_state = res2.json()["state"]["visible"]
    assert first_state != second_state


def test_overlay_js_generation():
    st = {
        "text": "TEST OVERLAY",
        "font_family": "Montserrat",
        "font_size": 40,
        "font_color": "#FFFFFF",
        "font_opacity": 90,
        "outline_enabled": True,
        "outline_color": "#000000",
        "outline_width": 2,
        "box_enabled": True,
        "box_color": "#112233",
        "box_opacity": 80,
        "box_padding": 12,
        "box_radius": 6,
        "shadow_enabled": True,
        "shadow_color": "#000000",
        "shadow_blur": 8,
        "shadow_x": 2,
        "shadow_y": 3,
        "pos_x": 15.0,
        "pos_y": 80.0,
        "anchor": "bottom-left",
        "visible": True,
    }
    js = overlay.generate_overlay_js(st)
    assert "obs-text-overlay" in js
    assert "TEST OVERLAY" in js
    assert "Montserrat" in js
    assert "-webkit-text-stroke" in js
    assert "text-shadow" in js


# --- 2. Emergency Failover BRB Slate Tests ---

def test_slate_brb_get_and_post(client):
    res = client.get("/api/slate/brb")
    assert res.status_code == 200
    assert "active" in res.json()

    # Show BRB slate
    res = client.post(
        "/api/slate/brb",
        json={"action": "show", "title": "Be Right Back", "subtitle": "Stream resuming in 5 mins"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert res.json()["state"]["active"] is True
    assert res.json()["state"]["title"] == "Be Right Back"

    # Hide BRB slate
    res = client.post(
        "/api/slate/brb",
        json={"action": "hide"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert res.json()["state"]["active"] is False


def test_slate_brb_js_generation():
    st = {"active": True, "title": "Stream Will Resume Shortly", "subtitle": "Please stand by"}
    js = slate.generate_brb_js(st)
    assert "stream-brb-holding-card" in js
    assert "Stream Will Resume Shortly" in js


# --- 3. Telegram Alerts Tests ---

def test_telegram_alert_unconfigured(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {})
    token, chat_id = telegram.get_telegram_config()
    assert token == ""
    assert chat_id == ""
    assert telegram.send_alert("Test message") is False


def test_telegram_alert_configured_success(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {
        "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        "TELEGRAM_CHAT_ID": "987654321",
    })
    token, chat_id = telegram.get_telegram_config()
    assert token == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    assert chat_id == "987654321"

    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        assert telegram.alert_stream_started("Zoom Meeting") is True
        assert mock_post.called
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["chat_id"] == "987654321"
        assert "STREAM ON-AIR" in call_kwargs["json"]["text"]


def test_telegram_alert_high_dropped_frames_cooldown(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {
        "TELEGRAM_BOT_TOKEN": "fake_token",
        "TELEGRAM_CHAT_ID": "fake_chat",
    })
    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        # First alert fires
        ok1 = telegram.alert_high_dropped_frames(10, 100, 10.0, 25.0, 5000.0, force=True)
        assert ok1 is True

        # Second alert in cooldown without force is suppressed
        ok2 = telegram.alert_high_dropped_frames(15, 100, 15.0, 25.0, 5000.0, force=False)
        assert ok2 is False


# --- 4. Local MP4 Recording Tests ---

def test_record_stream_api_status(client, monkeypatch):
    mock_status = {
        "recording": True,
        "file": "/home/zoombot/recordings/record-20260921-060000.mp4",
        "duration": 120,
        "size_mb": 45.2,
        "free_gb": 18.5,
        "halted_reason": "",
    }
    monkeypatch.setattr(control, "record_stream_action", lambda act: mock_status)

    res = client.get("/api/record")
    assert res.status_code == 200
    data = res.json()
    assert data["recording"] is True
    assert data["size_mb"] == 45.2


def test_record_stream_api_start_stop(client, monkeypatch):
    monkeypatch.setattr(control, "record_stream_action", lambda act: {"recording": act == "start"})

    res_start = client.post(
        "/api/record",
        json={"action": "start"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res_start.status_code == 200
    assert res_start.json()["recording"] is True

    res_stop = client.post(
        "/api/record",
        json={"action": "stop"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res_stop.status_code == 200
    assert res_stop.json()["recording"] is False
