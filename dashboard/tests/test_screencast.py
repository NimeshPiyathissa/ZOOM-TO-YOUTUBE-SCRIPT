"""Event-driven player-only preview (app/screencast.py), the player-only page mode and the Studio health payload.
MOCKED: a fake DevTools websocket and a stubbed page - this proves protocol handling, fan-out, bounded memory,
reconnect/backoff and idle shutdown, NOT how the real MJPhysics player or a real Chrome behaves."""
import asyncio
import base64
import json
import time

import pytest
from starlette.testclient import TestClient

from app import cdp, config, db, edumix, edumix_api, screencast, security, studio
from app.main import app

JPG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeWS:
    """Plays back DevTools screencast events and records everything the hub sends."""

    def __init__(self, frames=3, fail_after=None):
        self.sent, self.closed = [], False
        self.q = asyncio.Queue()
        for i in range(frames):
            self.q.put_nowait(json.dumps({"method": "Page.screencastFrame", "params": {
                "data": base64.b64encode(JPG + bytes([i])).decode(), "sessionId": 100 + i,
                "metadata": {"deviceWidth": 1280, "deviceHeight": 720}}}))
        self.fail_after = fail_after

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.fail_after is not None and len([m for m in self.sent if m["method"] == "Page.screencastFrameAck"]) >= self.fail_after:
            raise ConnectionError("devtools socket dropped")
        return await self.q.get()

    async def close(self):
        self.closed = True


@pytest.fixture
def target(monkeypatch):
    async def fake_target():
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/page/x"}
    monkeypatch.setattr(cdp, "_get_page_target", fake_target)


def test_viewer_limits_clamp_and_single_pending_frame():
    hub = screencast.Hub()

    async def go():
        vv = hub.add_viewer(fps=999, quality=1, width=99999)
        assert (vv.fps, vv.quality, vv.width) == (screencast.MAX_FPS, screencast.MIN_Q, screencast.MAX_W)
        vv.offer(b"a", 100.0); vv.offer(b"b", 100.5); vv.offer(b"c", 101.0)
        assert vv.dropped == 2 and vv.frame == b"c"          # never a queue: one pending frame, drops counted
        assert await vv.next(0.1) == b"c" and await vv.next(0.05) is None
        hub.remove_viewer(vv)
        hub.task.cancel()
    run(go())


def test_per_viewer_fps_cap():
    v = screencast.Viewer(fps=5, quality=55, width=1280)
    v.offer(b"a", 10.0); v.frame = None
    v.offer(b"b", 10.05)                                       # 50 ms later at 5 fps: skipped, not counted as a drop
    assert v.frame is None and v.dropped == 0
    v.offer(b"c", 10.25)
    assert v.frame == b"c"


def test_hub_streams_acks_every_frame_and_reports_stats(target):
    ws = FakeWS(frames=3)

    async def connect(url):
        return ws

    async def go():
        hub = screencast.Hub(connect=connect)
        v = hub.add_viewer(fps=30)
        got = []
        for _ in range(3):
            f = await v.next(2.0)
            if f is None:
                break
            got.append(f)
            await asyncio.sleep(0.04)
        snap = hub.snapshot()
        hub.remove_viewer(v); hub.task.cancel()
        return got, snap
    got, snap = run(go())
    methods = [m["method"] for m in ws.sent]
    assert methods[:2] == ["Page.enable", "Page.startScreencast"]
    assert methods.count("Page.screencastFrameAck") >= 2       # Chrome only sends more frames once acknowledged
    assert got and all(g.startswith(b"\xff\xd8") for g in got)
    assert snap["frames"] >= 2 and snap["geometry"] == {"w": 1280, "h": 720} and snap["capture_enabled"] is False


def test_no_viewers_means_no_capture_and_clean_stop(target):
    ws = FakeWS(frames=0)

    async def connect(url):
        return ws

    async def go():
        screencast.IDLE_STOP_SECONDS, old = 0.3, screencast.IDLE_STOP_SECONDS
        try:
            hub = screencast.Hub(connect=connect)
            v = hub.add_viewer()
            await asyncio.sleep(0.3)
            hub.remove_viewer(v)                               # "tab hidden / closed"
            await asyncio.wait_for(hub.task, 5)
            return hub
        finally:
            screencast.IDLE_STOP_SECONDS = old
    hub = run(go())
    assert hub.task.done() and ws.closed
    assert "Page.stopScreencast" in [m["method"] for m in ws.sent]


def test_dropped_devtools_connection_reconnects_with_bounded_backoff(target, monkeypatch):
    sockets = []
    delays = []

    async def connect(url):
        ws = FakeWS(frames=2, fail_after=1 if not sockets else None)
        sockets.append(ws)
        return ws

    real_sleep = asyncio.sleep

    async def fast_sleep(d, *a, **k):
        delays.append(d)
        await real_sleep(0)
    monkeypatch.setattr(screencast.asyncio, "sleep", fast_sleep)

    async def go():
        hub = screencast.Hub(connect=connect)
        v = hub.add_viewer(fps=30)
        for _ in range(200):
            await real_sleep(0.01)
            if len(sockets) >= 2 and hub.stats["frames"] >= 3:
                break
        hub.remove_viewer(v); hub.task.cancel()
        return hub
    hub = run(go())
    assert len(sockets) >= 2 and hub.stats["reconnects"] >= 1
    assert "ConnectionError" in hub.stats["last_error"]
    assert all(d <= 10 * 1.2 for d in delays)                  # backoff is capped


def test_too_many_viewers_refused():
    hub = screencast.Hub()

    async def go():
        vs = [hub.add_viewer() for _ in range(screencast.MAX_VIEWERS)]
        with pytest.raises(RuntimeError):
            hub.add_viewer()
        for v in vs:
            hub.remove_viewer(v)
        hub.task.cancel()
    run(go())


def test_multipart_framing():
    part = screencast.multipart_part(JPG)
    assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(JPG))
    assert part.endswith(JPG + b"\r\n")


def test_player_only_js_is_reversible_and_cosmetic():
    on, off = edumix.JS_PLAYER_ON, edumix.JS_PLAYER_OFF
    assert "position:fixed" in on and "zs-player-only-style" in on and "data-zs-player" in on
    assert "applied: false" in on                              # reverts itself when it can't isolate the player
    assert "zs-player-only-style" in off and "remove" in off
    for forbidden in ("cookie", "localStorage", "sessionStorage", "mediaKeys", "fetch(", "XMLHttpRequest", "src ="):
        assert forbidden not in on                             # never touches storage, media keys or the media source


def test_set_player_only_parses_page_result(monkeypatch):
    async def fake_eval(expr, user_gesture=False):
        return {"value": json.dumps({"applied": True, "kind": "iframe", "w": 1280, "h": 720})}
    monkeypatch.setattr(cdp, "evaluate", fake_eval)
    res = run(edumix.set_player_only(True))
    assert res["applied"] and res["kind"] == "iframe" and res["mode"] == "player"
    monkeypatch.setattr(cdp, "evaluate", lambda *a, **k: _async({"value": "garbage"}))
    assert run(edumix.set_player_only(True))["applied"] is False


async def _async(v):
    return v


def test_studio_sample_shape_and_no_foreign_chrome_counted():
    s = studio.sample()
    for key in ("host", "browser", "dashboard", "encoder", "health"):
        assert key in s
    assert s["host"]["mem_total_mb"] > 0 and s["dashboard"]["processes"] == 1
    assert studio.sample()["dashboard"]["cpu_percent"] >= 0
    # processes are matched by OUR DevTools port flag, never by name
    assert "--remote-debugging-port=" in open(studio.__file__, encoding="utf-8").read()


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
    screencast.hub.player_only = False
    return c


def test_preview_endpoints_require_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "anon.db")
    db.init_db()
    anon = TestClient(app)
    assert anon.get("/api/edumix/preview/stream").status_code == 401
    assert anon.get("/api/edumix/preview/stats").status_code == 401
    assert anon.get("/api/edumix/studio").status_code == 401
    assert anon.post("/api/edumix/preview/player-mode", json={"on": True}).status_code in (401, 403)


def test_player_mode_needs_csrf_and_records_intent(client, monkeypatch):
    async def fake(on):
        return {"applied": bool(on), "mode": "player" if on else "page"}
    monkeypatch.setattr(edumix, "set_player_only", fake)
    assert client.post("/api/edumix/preview/player-mode", json={"on": True}).status_code in (401, 403)
    r = client.post("/api/edumix/preview/player-mode", json={"on": True}, headers=client.csrf)
    assert r.status_code == 200 and r.json()["applied"] and screencast.hub.player_only is True
    client.post("/api/edumix/preview/player-mode", json={"on": False}, headers=client.csrf)
    assert screencast.hub.player_only is False


def test_player_mode_reports_unreachable_browser(client, monkeypatch):
    async def boom(on):
        raise cdp.CDPError("no browser")
    monkeypatch.setattr(edumix, "set_player_only", boom)
    assert client.post("/api/edumix/preview/player-mode", json={"on": True}, headers=client.csrf).status_code == 409


def test_studio_payload_has_preview_worker_and_locked_capture(client):
    d = client.get("/api/edumix/studio").json()
    assert d["capture"]["enabled"] is False
    assert {"viewers", "fps", "kbps", "dropped", "reconnects"} <= set(d["preview"])
    assert "recovery" in d["worker"] and d["worker"]["external"] is False
    assert d["health"]["level"] in ("ok", "warn", "bad", "idle")


def test_stats_endpoint(client):
    d = client.get("/api/edumix/preview/stats").json()
    assert d["capture_enabled"] is False and d["viewers"] == 0


def test_page_wires_view_switch_and_studio_panel(client):
    html = client.get("/edumix").text
    for needle in ('id="em-view-seg"', 'data-view="player"', 'data-view="page"', 'id="em-studio-h"', 'id="st-fps"'):
        assert needle in html
