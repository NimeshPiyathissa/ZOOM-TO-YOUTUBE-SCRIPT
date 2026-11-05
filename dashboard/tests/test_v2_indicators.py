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
