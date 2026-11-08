"""Overview indicators (meeting / YouTube broadcast), stale-state honesty and the dismissable broadcast error."""
import time

import pytest
from starlette.testclient import TestClient

from app import broadcast, config, db, deps, security, status
from app.main import app


def kinds(rows):
    return {r["unit"]: r["status"]["kind"] for r in rows}


def test_meeting_state_is_unknown_until_checked_and_when_stale():
    status.remember_zoom(None)
    assert kinds(status.pipeline_indicators({"state": "OFFLINE"}))["zoom-meeting"] == "unknown"
    t = time.time()
    status.remember_zoom({"status": "in_meeting", "detail": "ok", "confidence": "high"}, now=t)
    assert kinds(status.pipeline_indicators({"state": "OFFLINE"}, now=t + 5))["zoom-meeting"] == "active"
    assert kinds(status.pipeline_indicators({"state": "OFFLINE"}, now=t + status.ZOOM_FRESH_SECONDS + 1))["zoom-meeting"] == "unknown"


@pytest.mark.parametrize("zs,kind", [("connecting", "connecting"), ("not_joined", "idle"), ("expired", "error"), ("unknown", "unknown")])
def test_meeting_state_mapping(zs, kind):
    t = time.time()
    status.remember_zoom({"status": zs, "detail": "d", "confidence": "low"}, now=t)
    assert kinds(status.pipeline_indicators({"state": "OFFLINE"}, now=t))["zoom-meeting"] == kind


@pytest.mark.parametrize("bs,kind", [("LIVE", "on_air"), ("ERROR", "failed"), ("OFFLINE", "off_air"), ("READY", "off_air"), ("STARTING", "starting"), (None, "unknown")])
def test_broadcast_row(bs, kind):
    row = {r["unit"]: r for r in status.pipeline_indicators({"state": bs, "last_error": {"reason": "ffmpeg died"}})}["youtube-broadcast"]
    assert row["status"]["kind"] == kind
    if bs == "ERROR":
        assert "NOT restarted" in row["status"]["reason"] and "ffmpeg died" in row["status"]["reason"]


def test_every_new_kind_has_a_label_and_a_badge_class():
    js = open(__import__("pathlib").Path(__file__).resolve().parents[1] / "static" / "app.js", encoding="utf-8").read()
    for k in ("connecting", "unknown", "failed"):
        assert k in status.LABELS and f"{k}:" in js


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "i.db")
    db.init_db()
    broadcast.reset_for_tests()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    deps._rate_buckets.clear()
    return c


def test_error_can_be_dismissed_and_dismiss_needs_csrf_and_starts_nothing(client):
    broadcast.fail_session("ffmpeg exited (code 1)", {})
    assert broadcast.last_error()
    assert client.post("/api/broadcast/acknowledge-error").status_code in (400, 403)
    assert broadcast.last_error()                                   # refused without the CSRF token
    assert client.post("/api/broadcast/acknowledge-error", headers=client.csrf).json() == {"ok": True}
    assert broadcast.last_error() is None
    assert TestClient(app).post("/api/broadcast/acknowledge-error").status_code in (401, 403)


def test_drop_percent():
    from app import stats
    assert stats.drop_percent(0, 1000) == 0.0 and stats.drop_percent(0, 0) == 0.0
    assert stats.drop_percent(10, 990) == 1.0


def test_service_reasons_explain_stopped_zoom_and_browser_activity():
    def show(unit, phase="STOPPED", active="inactive", sub="dead"):
        return {"unit": unit, "phase": phase, "active_state": active, "sub_state": sub, "result": "success"}
    web_zoom = status.classify_unit(show("zoom"), "browser-source", "zoom")
    assert web_zoom["kind"] == "idle" and "web client" in web_zoom["reason"]
    needed = status.classify_unit(show("zoom"), "zoom", "zoom")
    assert needed["kind"] == "stopped" and "Join" in needed["reason"] and "never goes on air" in needed["reason"]
    running = status.classify_unit(show("browser-source", "LIVE", "active", "running"), "browser-source", "zoom")
    assert running["kind"] == "active" and "Zoom web client" in running["reason"] and "not the same as on air" in running["reason"]
    em = status.classify_unit(show("browser-source", "LIVE", "active", "running"), "browser-source", "edumix")
    assert "capture disabled" in em["reason"]


def test_broadcast_row_explains_authorization_state():
    row = lambda b: {r["unit"]: r for r in status.pipeline_indicators(b)}["youtube-broadcast"]["status"]
    assert "No source is ready" in row({"state": "OFFLINE"})["reason"]
    assert "source is ready" in row({"state": "READY"})["reason"]
    assert "confirmation is open" in row({"state": "READY", "grants_outstanding": 1})["reason"]
    adopted = row({"state": "LIVE", "adopted": True})
    assert adopted["kind"] == "on_air" and "before Go Live authorization" in adopted["reason"]


def test_recent_drop_rate_reflects_a_late_burst_and_resets_per_process():
    from app import stats
    stats._drop_samples.clear()
    t = 1000.0
    stats.recent_drop(7, 100000, 10, now=t)                         # long stream, few drops so far
    r = stats.recent_drop(7, 100900, 110, now=t + 30)               # 100 drops in the last 30 s
    assert r["drop_recent"] == 100 and r["drop_recent_pct"] == 10.0
    assert stats.drop_percent(110, 100900) < 0.2                    # the cumulative figure would hide it
    r = stats.recent_drop(8, 50, 0, now=t + 31)                     # new encoder process: no carry-over
    assert r["drop_recent"] == 0 and r["drop_window_seconds"] == 0.0
