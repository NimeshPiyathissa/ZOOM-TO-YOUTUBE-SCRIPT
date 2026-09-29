"""Unit tests for Remote GUI (/vnc) page, WebSocket proxy endpoints (/vnc/ws, /ws/vnc),
and VNC password configuration handling."""
import os
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.main import app
from app import db, config, settings_store, vncauth


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_dashboard.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    db.init_db()

    # Create test user and session
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


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_dashboard.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    db.init_db()
    return TestClient(app)


def test_vnc_page_requires_auth(anon_client):
    res = anon_client.get("/vnc", follow_redirects=False)
    assert res.status_code in (302, 303, 307)
    assert "/login" in res.headers["location"]


def test_vnc_page_authenticated(client):
    res = client.get("/vnc")
    assert res.status_code == 200
    html = res.text
    assert "Remote GUI" in html
    assert "vnc-screen" in html
    assert "vnc-config" in html
    assert "vnc.js" in html
    assert "DISPLAY :99" in html


def test_vnc_ws_unauthenticated_rejects(anon_client):
    with pytest.raises(Exception):
        with anon_client.websocket_connect("/vnc/ws"):
            pass
    with pytest.raises(Exception):
        with anon_client.websocket_connect("/ws/vnc"):
            pass


def test_vnc_password_settings_store_integration(tmp_path, monkeypatch):
    test_settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", test_settings_file)

    # 1. Test fallback to .env when settings.json has no vnc_password
    with patch("app.env_store.read_parsed", return_value={"VNC_PASSWORD": "env-password-123"}):
        s = settings_store.load_settings()
        assert s["vnc_password"] == "env-password-123"
        assert vncauth.current_password() == "env-password-123"

    # 2. Test settings.json override
    settings_store.save_settings({"vnc_password": "settings-custom-pass"})
    with patch("app.env_store.read_parsed", return_value={"VNC_PASSWORD": "env-password-123"}):
        s2 = settings_store.load_settings()
        assert s2["vnc_password"] == "settings-custom-pass"
        assert vncauth.current_password() == "settings-custom-pass"
