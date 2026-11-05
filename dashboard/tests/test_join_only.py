"""'Join only': joining a library meeting must be possible WITHOUT starting the encoder (nothing goes to YouTube), and a
join whose systemctl call fails must say so instead of looking like success. MOCKED: unit_action/unit_show are stubs."""
import time

import pytest
from starlette.testclient import TestClient

from app import config, control, db, security, sources
from app.main import app

ZOOM = {"id": 1, "name": "Test meeting", "type": "zoom", "url": "https://us06web.zoom.us/j/81234567890?pwd=abc",
        "options": {"join_method": "client"}}


@pytest.fixture
def rec(monkeypatch):
    class R:
        calls = []
        fail = set()
    r = R()
    r.calls = []

    def unit_action(unit, verb):
        r.calls.append((unit, verb))
        ok = (unit, verb) not in r.fail
        return {"ok": ok, "unit": unit, "verb": verb, "stderr": "" if ok else "Unit failed to start"}
    monkeypatch.setattr(control, "unit_action", unit_action)
    monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": "zoom"})
    monkeypatch.setattr(control, "apply_source_config", lambda s: None)
    monkeypatch.setattr(control, "_set_slate", lambda: {"ok": True, "action": "slate"})
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    r.up = False
    monkeypatch.setattr(control, "_ffmpeg_is_up", lambda: r.up)
    return r


def test_default_join_still_starts_the_encoder(rec):
    out = control.start_source(ZOOM)
    assert ("ffmpeg-stream", "start") in rec.calls and out["encoder_started"] is True


def test_join_only_never_starts_the_encoder(rec):
    out = control.start_source(ZOOM, start_encoder=False)
    assert not [c for c in rec.calls if c[0] == "ffmpeg-stream"]
    assert ("zoom", "restart") in rec.calls and out["encoder_started"] is False


def test_join_only_leaves_a_running_encoder_alone(rec):
    rec.up = True
    control.start_source(ZOOM, start_encoder=False)
    assert not [c for c in rec.calls if c[0] == "ffmpeg-stream"]


def test_a_failed_producer_start_is_reported(rec):
    rec.fail = {("zoom", "restart")}
    out = control.start_source(ZOOM, start_encoder=False)
    assert out["failed"] == [{"unit": "zoom", "verb": "restart", "error": "Unit failed to start"}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "j.db")
    db.init_db()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    from app import deps
    deps._rate_buckets.clear()
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    return c


def test_switch_api_passes_go_live_through(client, monkeypatch):
    seen = []
    monkeypatch.setattr(control, "start_source", lambda s, go=True: seen.append(go) or
                        {"results": [], "rtmp_dropped": False, "hot_swapped": False, "failed": [], "encoder_started": go})
    import app.main as m
    monkeypatch.setattr(m, "_schedule_zoom_followup", lambda *a, **k: None)
    sid = sources.create_source("A", "zoom", "https://us06web.zoom.us/j/81234567890?pwd=abc", {})
    assert client.post(f"/api/sources/{sid}/switch", headers=client.csrf).json()["encoder_started"] is True       # default: unchanged
    assert client.post(f"/api/sources/{sid}/switch", json={"go_live": False}, headers=client.csrf).json()["encoder_started"] is False
    assert seen == [True, False]


def test_join_button_that_systemctl_refuses_is_an_error(client, monkeypatch):
    sid = sources.create_source("A", "zoom", "https://us06web.zoom.us/j/81234567890?pwd=abc", {})
    sources.set_active_source_id(sid)
    monkeypatch.setattr(control, "zoom_join", lambda action, active: {"ok": False, "stderr": "Failed to start zoom.service"})
    r = client.post("/api/zoom/rejoin", headers=client.csrf)
    assert r.status_code == 400 and "Failed to start" in r.json()["error"]
