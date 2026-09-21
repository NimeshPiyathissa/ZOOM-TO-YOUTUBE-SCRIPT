"""Tests for YouTube Live Studio Room (/studio), telemetry, audio ladder,
stream key manager, and live chat settings."""
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient

from app.main import app
from app import db, config, audio_level, stats, env_store


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


def test_studio_page_requires_auth(anon_client):
    res = anon_client.get("/studio", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_studio_page_authenticated(client, monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "test-key-1234"})
    res = client.get("/studio")
    assert res.status_code == 200
    html = res.text
    assert "YouTube Live Studio" in html
    assert "OFF-AIR" in html
    assert "GO LIVE" in html
    assert "RTMP Ingest URL" in html
    assert "rtmps://a.rtmps.youtube.com:443/live2" in html
    assert "PulseAudio Peak Monitor" in html
    assert "CH 1 · L" in html
    assert "CH 2 · R" in html
    assert "YouTube Live Chat" in html


def test_studio_stream_key_reveal_unauthenticated(anon_client):
    res = anon_client.get("/api/studio/stream-key/reveal")
    assert res.status_code == 401


def test_studio_stream_key_reveal_authenticated(client, monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "super-secret-key"})
    res = client.get("/api/studio/stream-key/reveal")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    data = res.json()
    assert data["stream_key"] == "super-secret-key"
    assert data["rtmp_url"] == "rtmps://a.rtmps.youtube.com:443/live2"


def test_studio_stream_key_save_validation(client):
    # Empty key rejected
    res = client.post(
        "/api/studio/stream-key",
        json={"stream_key": "   "},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 400


def test_studio_stream_key_save_success(client, monkeypatch):
    monkeypatch.setattr(env_store, "write_updates", lambda u: ["ffmpeg-stream"])
    res = client.post(
        "/api/studio/stream-key",
        json={"stream_key": "abcd-efgh-ijkl-mnop"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["units_to_restart"] == ["ffmpeg-stream"]
    assert "mnop" in data["hint"]


def test_studio_settings_get_and_save(client):
    # Default settings
    res = client.get("/api/studio/settings")
    assert res.status_code == 200
    assert "control_room_url" in res.json()

    # Save settings with raw video ID
    res = client.post(
        "/api/studio/settings",
        json={"video_id": "V_V6jyJ9RZ0"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert res.json()["video_id"] == "V_V6jyJ9RZ0"
    assert "https://www.youtube.com/watch?v=V_V6jyJ9RZ0" in res.json()["watch_url"]

    # Save settings with full YouTube watch URL
    res = client.post(
        "/api/studio/settings",
        json={"video_id": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"X-CSRF-Token": client.session["csrf_token"]},
    )
    assert res.status_code == 200
    assert res.json()["video_id"] == "dQw4w9WgXcQ"


def test_audio_level_dual_channel_snapshot():
    # Verify manager snapshot produces peak_l_db and peak_r_db
    mgr = audio_level.AudioLevelManager()
    mgr.latest = {
        "peak_db": -12.5,
        "peak_l_db": -14.0,
        "peak_r_db": -12.5,
        "rms_db": -18.2,
        "rms_l_db": -19.5,
        "rms_r_db": -18.2,
    }
    mgr.latest_at = time.time()
    snap = mgr.snapshot()
    assert snap["live"] is True
    assert snap["peak_db"] == -12.5
    assert snap["peak_l_db"] == -14.0
    assert snap["peak_r_db"] == -12.5
    assert snap["rms_db"] == -18.2


def test_audio_level_stereo_fallback_snapshot():
    # If sampler only returned peak_db (older version fallback)
    mgr = audio_level.AudioLevelManager()
    mgr.latest = {"peak_db": -9.0, "rms_db": -15.0}
    mgr.latest_at = time.time()
    snap = mgr.snapshot()
    assert snap["live"] is True
    assert snap["peak_db"] == -9.0
    assert snap["peak_l_db"] == -9.0
    assert snap["peak_r_db"] == -9.0


def test_stats_ffmpeg_progress_encoder_cpu(tmp_path, monkeypatch):
    log_file = tmp_path / "ffmpeg.log"
    log_file.write_text(
        "frame=  150 fps= 30.0 q=28.0 size=    1250kB time=00:00:05.00 bitrate=2048.0kbits/s speed=1.00x\n"
    )
    monkeypatch.setattr(config, "FFMPEG_LOG", log_file)
    monkeypatch.setattr(stats, "_get_encoder_cpu", lambda pid: 22.5)

    show = {"phase": "LIVE", "main_pid": 1234}
    progress = stats.ffmpeg_progress(show)
    assert progress is not None
    assert progress["frame"] == 150
    assert progress["fps"] == 30.0
    assert progress["bitrate_kbps"] == 2048.0
    assert progress["encoder_cpu"] == 22.5


def test_api_zoom_clean_feed_requires_auth(anon_client):
    res = anon_client.post("/api/zoom/clean-feed")
    assert res.status_code == 401


def test_api_zoom_clean_feed_post(client, monkeypatch):
    from app import zoom_web
    async def mock_cleanfeed():
        return {"ok": True, "result": True}
    monkeypatch.setattr(zoom_web, "apply_cleanfeed_async", mock_cleanfeed)

    # Missing CSRF
    res = client.post("/api/zoom/clean-feed")
    assert res.status_code == 403

    # With CSRF
    res = client.post("/api/zoom/clean-feed", headers={"x-csrf-token": client.session["csrf_token"]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "result": True}

    # GET variant
    res_get = client.get("/api/zoom/clean-feed")
    assert res_get.status_code == 200
    assert res_get.json() == {"ok": True, "result": True}

