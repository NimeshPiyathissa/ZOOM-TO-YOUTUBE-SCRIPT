"""Service status that MEANS something (app/status.py) and start/stop buttons that cannot silently "succeed".
MOCKED: unit_show / unit_action / zoom status are stubbed - this proves the classification and the API contract, not
systemd itself (the real unit states were compared against production by hand, see docs/zoom-hotfix.md)."""
import time

import pytest
from starlette.testclient import TestClient

from app import config, control, db, security, sources, stats, status
from app.main import app


def show(unit, active="inactive", sub="dead", phase="STOPPED", **kw):
    return {"unit": unit, "active_state": active, "sub_state": sub, "phase": phase, "result": "success",
            "unit_file_state": "enabled", **kw}


# ------------------------------------------------------------------ classification

def test_idle_encoder_is_off_air_not_a_failure():
    r = status.classify_unit(show("ffmpeg-stream"), None)
    assert r["kind"] == "off_air" and r["label"] == "Off air" and "normal" in r["reason"]


def test_browser_source_idle_when_zoom_client_is_the_source_but_stopped_when_needed():
    assert status.classify_unit(show("browser-source"), "zoom", "zoom")["kind"] == "idle"
    assert status.classify_unit(show("browser-source"), None, None)["kind"] == "idle"
    need = status.classify_unit(show("browser-source"), "browser-source", "webpage")
    assert need["kind"] == "stopped" and "needs the browser" in need["reason"]


def test_zoom_client_idle_for_web_sources_stopped_when_it_is_the_source():
    assert status.classify_unit(show("zoom"), "browser-source", "webpage")["kind"] == "idle"
    assert status.classify_unit(show("zoom"), "zoom", "zoom")["kind"] == "stopped"


def test_required_units_down_is_a_problem_and_running_ones_are_active():
    assert status.classify_unit(show("xvfb"), None)["kind"] == "stopped"
    assert status.classify_unit(show("x11vnc", "active", "running", "LIVE"), None)["kind"] == "active"
    one_shot = status.classify_unit(show("audio-setup", "active", "exited", "LIVE"), None)
    assert one_shot["kind"] == "active" and "One-shot" in one_shot["reason"]


def test_encoder_states():
    assert status.classify_unit(show("ffmpeg-stream", "active", "running", "LIVE"), None)["kind"] == "on_air"
    assert status.classify_unit(show("ffmpeg-stream", "activating", "start", "STARTING"), None)["kind"] == "starting"
    assert status.classify_unit(show("ffmpeg-stream", "activating", "auto-restart", "RECONNECTING"), None)["kind"] == "retrying"
    assert status.classify_unit(show("ffmpeg-stream", "failed", "failed", "FAILED"), None)["kind"] == "error"


def test_missing_unit_is_not_configured():
    r = status.classify_unit(show("novnc-proxy", unit_file_state="not-found"), None)
    assert r["kind"] == "not_configured"


def test_annotate_adds_status_and_title_to_every_row():
    rows = status.annotate_units([show("ffmpeg-stream"), show("zoom", "active", "running", "LIVE")], "zoom", "zoom")
    assert [r["status"]["kind"] for r in rows] == ["off_air", "active"]
    assert rows[0]["title"] == "Encoder to YouTube"


def test_system_service_states_including_disconnected_worker(monkeypatch):
    monkeypatch.setattr(config, "EDUMIX_WORKER_EXTERNAL", True)
    now = 1000.0
    assert status.classify_system("caddy", "active")["kind"] == "active"
    assert status.classify_system("caddy", "failed")["kind"] == "error"
    assert status.classify_system("caddy", "unavailable")["kind"] == "not_configured"
    assert status.classify_system("edumix-worker", "active", job=None, now=now)["kind"] == "idle"
    job = {"completed_at": None, "desired": "running", "heartbeat": now - 5}
    assert status.classify_system("edumix-worker", "active", job=job, now=now)["kind"] == "active"
    job["heartbeat"] = now - 120
    assert status.classify_system("edumix-worker", "active", job=job, now=now)["kind"] == "disconnected"
    assert status.classify_system("edumix-worker", "inactive")["kind"] == "stopped"
    monkeypatch.setattr(config, "EDUMIX_WORKER_EXTERNAL", False)
    assert status.classify_system("edumix-worker", "inactive")["kind"] == "not_configured"


def test_system_services_are_cached_so_polling_stays_cheap(monkeypatch):
    calls = []
    monkeypatch.setattr(status, "_is_active", lambda unit: calls.append(unit) or "active")
    status._CACHE.update(at=0.0, value=None)
    a = status.system_services(now=100.0)
    b = status.system_services(now=105.0)
    assert a is b and len(calls) == 2                      # caddy + worker, once; dashboard is this process
    status.system_services(now=111.0)
    assert len(calls) == 4


# ------------------------------------------------------------------ API

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    from app import deps
    deps._rate_buckets.clear()
    status._CACHE.update(at=0.0, value=None)
    monkeypatch.setattr(status, "_is_active", lambda unit: "active")
    return c


def test_state_endpoint_reports_meaning_for_every_unit(client, monkeypatch):
    def fake_show(unit):
        live = unit not in ("ffmpeg-stream", "browser-source")
        return show(unit, "active" if live else "inactive", "running" if live else "dead", "LIVE" if live else "STOPPED",
                    restart_count=0, uptime_seconds=5 if live else None, main_pid=7 if live else 0)
    monkeypatch.setattr(control, "unit_show", fake_show)
    monkeypatch.setattr(stats, "ffmpeg_progress", lambda s: None)
    monkeypatch.setattr(stats, "system_stats", lambda: {})
    d = client.get("/api/state").json()
    kinds = {u["unit"]: u["status"]["kind"] for u in d["units"]}
    assert kinds["ffmpeg-stream"] == "off_air" and kinds["browser-source"] == "idle" and kinds["zoom"] == "active"
    assert {s["unit"] for s in d["services"]} == {"dashboard", "caddy", "edumix-worker"}
    assert next(s for s in d["services"] if s["unit"] == "dashboard")["status"]["kind"] == "active"


async def _no_sleep(*a, **k):
    return None


def test_preflight_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "anon.db")
    db.init_db()
    assert TestClient(app).get("/api/stream/preflight").status_code == 401


def test_boot_readiness_accepts_a_manual_start_encoder_but_not_a_missing_one(monkeypatch):
    def units(ffmpeg_state):
        return lambda unit: show(unit, "active", "running", "LIVE",
                                 unit_file_state=ffmpeg_state if unit == "ffmpeg-stream" else "enabled")
    for state, expect in (("disabled", True), ("enabled", True), ("not-found", False), ("", False)):
        monkeypatch.setattr(control, "unit_show", units(state))
        checks = control.reboot_readiness()
        enc = next(c for c in checks if c["check"].startswith("ffmpeg-stream"))
        assert enc["ok"] is expect, (state, enc)
        others = [c for c in checks if c["check"].endswith(".service enabled") and not c["check"].startswith("dashboard") and not c["check"].startswith("caddy")]
        assert others and all(c["ok"] for c in others)             # every other unit must still be enabled at boot
    monkeypatch.setattr(control, "unit_show", lambda unit: show(unit, "active", "running", "LIVE", unit_file_state="disabled"))
    checks = control.reboot_readiness()
    assert not next(c for c in checks if c["check"] == "xvfb.service enabled")["ok"]
