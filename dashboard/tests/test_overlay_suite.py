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
from app import sources as sources_mod


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
    assert "livestream-watermark-overlay" in js
    assert "livestream-watermark-inner" in js
    assert "document.documentElement" in js
    assert "MutationObserver" in js
    assert "2147483647" in js
    assert "display: block !important" in js
    assert "TEST OVERLAY" in js
    assert "Montserrat" in js
    assert "-webkit-text-stroke" in js
    assert "text-shadow" in js

    # Test hidden state
    st_hidden = dict(st, visible=False)
    js_hidden = overlay.generate_overlay_js(st_hidden)
    assert "display: none !important" in js_hidden


def test_extended_typography_suite(client):
    assert len(overlay.GOOGLE_FONTS) >= 60
    assert "High-Impact & Broadcast Titles" in overlay.GOOGLE_FONT_CATEGORIES
    assert "Modern & Clean Sans-Serif" in overlay.GOOGLE_FONT_CATEGORIES
    assert "Condensed & Tall" in overlay.GOOGLE_FONT_CATEGORIES
    assert "Tech, Sci-Fi & Gaming" in overlay.GOOGLE_FONT_CATEGORIES
    assert "Elegant & Editorial Serif" in overlay.GOOGLE_FONT_CATEGORIES
    assert "Handwritten & Script" in overlay.GOOGLE_FONT_CATEGORIES
    assert "Sri Lankan / Sinhala Unicode Support" in overlay.GOOGLE_FONT_CATEGORIES

    # Check Sinhala Unicode fonts
    sinhala_fonts = overlay.GOOGLE_FONT_CATEGORIES["Sri Lankan / Sinhala Unicode Support"]
    assert "Noto Sans Sinhala" in sinhala_fonts
    assert "Noto Serif Sinhala" in sinhala_fonts
    assert "Abhaya Libre" in sinhala_fonts

    # Test dynamic JS generation for special font names
    js_sinhala = overlay.generate_overlay_js({"font_family": "Noto Sans Sinhala", "visible": True})
    assert "Noto+Sans+Sinhala" in js_sinhala
    assert "font-overlay-noto-sans-sinhala" in js_sinhala

    js_gaming = overlay.generate_overlay_js({"font_family": "Press Start 2P", "visible": True})
    assert "Press+Start+2P" in js_gaming
    assert "font-overlay-press-start-2p" in js_gaming

    # Test template dropdown renders optgroups
    res = client.get("/overlay")
    assert res.status_code == 200
    html = res.text
    assert '<optgroup label="High-Impact &amp; Broadcast Titles">' in html or '<optgroup label="High-Impact & Broadcast Titles">' in html
    assert '<optgroup label="Sri Lankan / Sinhala Unicode Support">' in html
    assert "Noto Sans Sinhala" in html


def test_visual_font_picker_suite(client):
    preview_urls = overlay.get_google_fonts_preview_urls()
    assert len(preview_urls) >= 3
    assert all("fonts.googleapis.com/css2" in u for u in preview_urls)
    assert all("display=swap" in u for u in preview_urls)

    res = client.get("/overlay")
    assert res.status_code == 200
    html = res.text
    assert "custom-font-picker" in html
    assert "font-picker-trigger" in html
    assert "font-picker-search" in html
    assert "specimen-badge" in html
    assert "අආ ශ්‍රී" in html
    assert "Ag 123" in html
    assert 'data-font="Noto Sans Sinhala"' in html
    assert 'data-font="Anton"' in html
    assert 'name="fontFamily"' in html



def test_overlay_api_status(client, monkeypatch):
    from app import cdp

    async def mock_get_page_target():
        return {
            "title": "Zoom Meeting Session",
            "url": "https://app.zoom.us/wc/123456/join",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1",
        }

    async def mock_evaluate(expression, user_gesture=False):
        return {
            "value": {
                "injected": True,
                "visible": True,
                "parent": "HTML",
                "text": "TEST OVERLAY",
            }
        }

    monkeypatch.setattr(cdp, "_get_page_target", mock_get_page_target)
    monkeypatch.setattr(cdp, "evaluate", mock_evaluate)

    res = client.get("/api/overlay/status")
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["injected"] is True
    assert data["visible"] is True
    assert data["target_title"] == "Zoom Meeting Session"
    assert data["parent"] == "HTML"


def test_overlay_api_reinject(client, monkeypatch):
    from app import cdp

    async def mock_get_page_target():
        return {
            "title": "Zoom Meeting Session",
            "url": "https://app.zoom.us/wc/123456/join",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1",
        }

    async def mock_evaluate(expression, user_gesture=False):
        return {
            "value": {
                "injected": True,
                "visible": True,
                "parent": "HTML",
                "text": "TEST OVERLAY",
            }
        }

    monkeypatch.setattr(cdp, "_get_page_target", mock_get_page_target)
    monkeypatch.setattr(cdp, "evaluate", mock_evaluate)

    res = client.post(
        "/api/overlay/reinject",
        json={},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["status"]["connected"] is True
    assert data["status"]["injected"] is True
    assert data["status"]["pushed"] is True


# --- 1b. Part 4: real (encoder-burned) watermark wiring ---

def test_overlay_save_writes_real_watermark_config(client, monkeypatch):
    """The actual fix under test: saving the overlay must reach the
    encoder-consumed config (current-source.env's WATERMARK_* keys), not
    just overlay.json."""
    calls = []
    monkeypatch.setattr(control, "write_watermark_config", lambda updated: calls.append(updated))
    monkeypatch.setattr(sources_mod, "get_active_source", lambda: None)
    monkeypatch.setattr(control, "watermark_is_running", lambda: False)

    res = client.post(
        "/api/overlay",
        json={"text": "ACME", "visible": True},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert len(calls) == 1
    assert calls[0]["visible"] is True
    assert calls[0]["text"] == "ACME"
    assert res.json()["requires_restart"] is True  # saved visible=True, encoder reports not running


def test_overlay_save_no_restart_flag_when_already_matching(client, monkeypatch):
    monkeypatch.setattr(control, "write_watermark_config", lambda updated: None)
    monkeypatch.setattr(sources_mod, "get_active_source", lambda: None)
    monkeypatch.setattr(control, "watermark_is_running", lambda: True)

    res = client.post(
        "/api/overlay",
        json={"visible": True},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.json()["requires_restart"] is False


def test_overlay_save_auto_switches_copy_mode_direct_source(client, monkeypatch):
    """Part 4's decision: enabling the watermark on a copy-mode direct
    source auto-switches it to re-encode, since a burned-in filter can't
    ride along with stream copy."""
    active_source = {
        "id": 7, "name": "My Stream", "type": "direct", "url": "https://example.com/live.m3u8",
        "options": {"mode": "copy"}, "account_id": None,
    }
    monkeypatch.setattr(sources_mod, "get_active_source", lambda: active_source)
    update_calls = []
    monkeypatch.setattr(
        sources_mod, "update_source",
        lambda sid, name, type_, url, options, account_id=None: update_calls.append((sid, options)),
    )
    monkeypatch.setattr(control, "write_watermark_config", lambda updated: None)
    monkeypatch.setattr(control, "write_current_source", lambda source: None)
    monkeypatch.setattr(control, "watermark_is_running", lambda: False)

    res = client.post(
        "/api/overlay",
        json={"visible": True},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert len(update_calls) == 1
    assert update_calls[0][0] == 7
    assert update_calls[0][1]["mode"] == "reencode"


def test_overlay_save_leaves_reencode_direct_source_alone(client, monkeypatch):
    active_source = {
        "id": 7, "name": "My Stream", "type": "direct", "url": "https://example.com/live.m3u8",
        "options": {"mode": "reencode"}, "account_id": None,
    }
    monkeypatch.setattr(sources_mod, "get_active_source", lambda: active_source)
    update_calls = []
    monkeypatch.setattr(
        sources_mod, "update_source",
        lambda *a, **k: update_calls.append((a, k)),
    )
    monkeypatch.setattr(control, "write_watermark_config", lambda updated: None)
    monkeypatch.setattr(control, "write_current_source", lambda source: None)
    monkeypatch.setattr(control, "watermark_is_running", lambda: False)

    client.post(
        "/api/overlay",
        json={"visible": True},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert update_calls == []  # already re-encode - nothing to switch


def test_overlay_save_applies_watermark_even_when_pipeline_write_fails(client, monkeypatch):
    """A locked-down/undeployed pipeline must not turn saving overlay
    settings into a 500 - see main.py's try/except around
    _apply_watermark_config."""
    def boom(updated):
        raise control.ControlError("sudo not available")
    monkeypatch.setattr(control, "write_watermark_config", boom)
    monkeypatch.setattr(sources_mod, "get_active_source", lambda: None)
    monkeypatch.setattr(control, "watermark_is_running", lambda: False)

    res = client.post(
        "/api/overlay",
        json={"visible": True},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_overlay_status_reports_encoder_active_and_requires_restart(client, monkeypatch):
    from app import cdp

    async def mock_get_page_target():
        raise RuntimeError("no kiosk running")

    monkeypatch.setattr(cdp, "_get_page_target", mock_get_page_target)
    monkeypatch.setattr(control, "watermark_is_running", lambda: True)
    overlay.save_overlay_state({"visible": False})  # configured_visible=False, encoder still running=True

    res = client.get("/api/overlay/status")
    assert res.status_code == 200
    data = res.json()
    assert data["encoder_active"] is True
    assert data["requires_restart"] is True  # mismatch between saved (False) and running (True)


def test_overlay_image_upload_rejects_bad_extension(client):
    res = client.post(
        "/api/overlay/image",
        files={"image": ("logo.gif", b"not-really-a-gif", "image/gif")},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 400


def test_overlay_image_upload_rejects_oversized_file(client):
    big = b"\x00" * (5 * 1024 * 1024 + 1)
    res = client.post(
        "/api/overlay/image",
        files={"image": ("logo.png", big, "image/png")},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 400


def test_overlay_image_upload_writes_via_zoombot_bridge(client, monkeypatch):
    monkeypatch.setattr(control, "write_watermark_image", lambda content, ext: "/home/zoombot/zoom-stream/watermarks/logo.png")

    res = client.post(
        "/api/overlay/image",
        files={"image": ("logo.png", b"\x89PNG\r\n fake but under the size limit", "image/png")},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["image_path"] == "/home/zoombot/zoom-stream/watermarks/logo.png"
    assert data["state"]["mode"] == "image"
    assert data["state"]["image_path"] == "/home/zoombot/zoom-stream/watermarks/logo.png"


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
