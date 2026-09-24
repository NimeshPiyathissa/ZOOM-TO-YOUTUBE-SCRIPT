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


def test_vnc_password_vault_takes_priority_over_settings_and_env(tmp_path, monkeypatch):
    """Part 0: the vault is the source of truth once an install has run
    setup/migration - it must win over both legacy fallbacks, not just be
    one more place to check."""
    from app import secret_store, crypto
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_DIR", tmp_path / "etc-zoom-stream")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_FILE", tmp_path / "etc-zoom-stream" / "master.key")
    monkeypatch.setattr(config, "VAULT_LOCK_SENTINEL_FILE", tmp_path / "vault.locked")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(crypto, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(crypto, "ARGON2_MEMORY_COST_KIB", 8192)
    monkeypatch.setattr(crypto, "ARGON2_PARALLELISM", 1)
    secret_store._key = None
    secret_store._secrets = None

    settings_store.save_settings({"vnc_password": "settings-stale-pass"})
    secret_store.initialize("pw", initial_secrets={"VNC_PASSWORD": "vault-current-pass"}, unlock_mode="prompt")

    try:
        assert vncauth.current_password() == "vault-current-pass"
    finally:
        secret_store._key = None
        secret_store._secrets = None


def test_vnc_password_falls_back_when_vault_locked(tmp_path, monkeypatch):
    from app import secret_store
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    secret_store._key = None
    secret_store._secrets = None

    settings_store.save_settings({"vnc_password": "settings-only-pass"})
    assert vncauth.current_password() == "settings-only-pass"


def test_rotate_vnc_password_updates_vault_not_settings_json_when_unlocked(tmp_path, monkeypatch):
    """Part 0 regression: rotating the VNC password must never write
    plaintext to settings.json again once the vault is the source of
    truth - that's exactly the exposure Part 0 closed."""
    from app import control, secret_store, crypto
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_DIR", tmp_path / "etc-zoom-stream")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_FILE", tmp_path / "etc-zoom-stream" / "master.key")
    monkeypatch.setattr(config, "VAULT_LOCK_SENTINEL_FILE", tmp_path / "vault.locked")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(crypto, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(crypto, "ARGON2_MEMORY_COST_KIB", 8192)
    monkeypatch.setattr(crypto, "ARGON2_PARALLELISM", 1)
    secret_store._key = None
    secret_store._secrets = None
    secret_store.initialize("pw", initial_secrets={"VNC_PASSWORD": "old-pass"}, unlock_mode="prompt")

    monkeypatch.setattr(control, "run_as_zoombot", lambda argv, input_bytes=None, timeout=15: MagicMock(returncode=0, stderr=b""))
    monkeypatch.setattr(control, "unit_action", lambda unit, action: {"unit": unit, "action": action})

    try:
        control.rotate_vnc_password("new-pass")
        assert secret_store.get_secret("VNC_PASSWORD") == "new-pass"
        assert settings_store.load_settings().get("vnc_password", "") != "new-pass"
    finally:
        secret_store._key = None
        secret_store._secrets = None


def test_rotate_vnc_password_falls_back_to_settings_when_vault_locked(tmp_path, monkeypatch):
    from app import control, secret_store
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    secret_store._key = None
    secret_store._secrets = None

    monkeypatch.setattr(control, "run_as_zoombot", lambda argv, input_bytes=None, timeout=15: MagicMock(returncode=0, stderr=b""))
    monkeypatch.setattr(control, "unit_action", lambda unit, action: {"unit": unit, "action": action})

    control.rotate_vnc_password("legacy-pass")
    assert settings_store.load_settings()["vnc_password"] == "legacy-pass"
