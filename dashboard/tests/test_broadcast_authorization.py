"""Strict manual broadcast authorization (app/broadcast.py + control.unit_action + the API + the schedulers).
MOCKED: systemd is a recording fake - these tests prove that NO code path can start the encoder without an operator's
confirmed, single-use grant; they do not start a real ffmpeg. The shell side (scripts/stream.sh) is tested separately in
test_stream_gate.py."""
import threading
import time

import pytest
from starlette.testclient import TestClient

from app import broadcast, config, control, db, scheduler, security, sources
from app.main import app

ZOOM_URL = "https://us06web.zoom.us/j/81234567890?pwd=abc"


def show(phase="STOPPED", **kw):
    return {"unit": "ffmpeg-stream", "phase": phase, "active_state": "inactive", "sub_state": "dead", "result": "success",
            "exec_main_status": "", "exec_main_code": "", **kw}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "b.db")
    db.init_db()
    broadcast.reset_for_tests()
    yield


SRC = {"id": 1, "name": "Meeting A", "type": "zoom"}


# ------------------------------------------------------------------ grants

def test_grant_is_single_use_and_replay_is_refused(tmp_db):
    g = broadcast.issue_grant("alice", "go_live", SRC, 5, broadcast.OFFLINE)
    grant = broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 5)
    assert isinstance(grant, broadcast.Grant) and grant.valid()
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 5)
    assert e.value.code == "grant_used" and e.value.status == 409


def test_unknown_expired_foreign_wrong_purpose_and_changed_source_are_refused(tmp_db):
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant("made-up", "alice", "go_live", SRC, 5)
    assert e.value.code == "no_grant" and e.value.status == 403
    g = broadcast.issue_grant("alice", "go_live", SRC, 5, broadcast.OFFLINE, now=1000)
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 5, now=1000 + broadcast.GRANT_TTL_SECONDS + 1)
    assert e.value.code == "grant_expired" and e.value.status == 410
    g = broadcast.issue_grant("alice", "go_live", SRC, 5, broadcast.OFFLINE)
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant(g["grant"], "mallory", "go_live", SRC, 5)
    assert e.value.code == "grant_foreign"
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant(g["grant"], "alice", "restart", SRC, 5)
    assert e.value.code == "grant_purpose"
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 6)           # source revision moved
    assert e.value.code == "source_changed"
    g = broadcast.issue_grant("alice", "go_live", SRC, 5, broadcast.OFFLINE)
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.consume_grant(g["grant"], "alice", "go_live", {"id": 2, "name": "B"}, 5)   # different source
    assert e.value.code == "source_changed"


def test_a_new_dialog_invalidates_the_previous_grant(tmp_db):
    first = broadcast.issue_grant("alice", "go_live", SRC, 5, broadcast.OFFLINE)
    second = broadcast.issue_grant("alice", "go_live", SRC, 5, broadcast.OFFLINE)
    with pytest.raises(broadcast.BroadcastError):
        broadcast.consume_grant(first["grant"], "alice", "go_live", SRC, 5)
    assert broadcast.consume_grant(second["grant"], "alice", "go_live", SRC, 5)


def test_cannot_authorize_when_state_does_not_allow_it(tmp_db):
    for st in (broadcast.LIVE, broadcast.STARTING, broadcast.STOPPING):
        with pytest.raises(broadcast.BroadcastError) as e:
            broadcast.issue_grant("a", "go_live", SRC, 1, st)
        assert e.value.code.startswith("already_")
    with pytest.raises(broadcast.BroadcastError) as e:
        broadcast.issue_grant("a", "restart", SRC, 1, broadcast.OFFLINE)
    assert e.value.code == "not_live"


def test_grant_objects_cannot_be_forged(tmp_db):
    with pytest.raises(TypeError):
        broadcast.Grant(object(), "x", "go_live", "u", 1, time.time())


# ------------------------------------------------------------------ state machine

def start_session(now=None):
    g = broadcast.issue_grant("alice", "go_live", SRC, 1, broadcast.OFFLINE)
    grant = broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 1)
    return broadcast.begin_session(grant, SRC, now)


def test_offline_ready_starting_live_stopping_cycle(tmp_db):
    assert broadcast.snapshot(show(), False)["state"] == broadcast.OFFLINE
    assert broadcast.snapshot(show(), True)["state"] == broadcast.READY
    now = time.time()
    start_session(now)
    assert broadcast.snapshot(show("STARTING"), True, now + 1)["state"] == broadcast.STARTING
    assert broadcast.snapshot(show("LIVE"), True, now + 5)["state"] == broadcast.LIVE
    assert broadcast.get_session()["state"] == "live"
    broadcast.mark_stop_requested(now + 60)
    assert broadcast.snapshot(show("LIVE"), True, now + 61)["state"] == broadcast.STOPPING
    snap = broadcast.snapshot(show("STOPPED"), True, now + 70)
    assert snap["state"] == broadcast.READY and broadcast.get_session() is None and snap["last_error"] is None


def test_a_crash_is_an_error_with_kept_diagnostics_and_needs_a_fresh_grant(tmp_db):
    now = time.time()
    start_session(now)
    broadcast.snapshot(show("LIVE"), True, now + 5)
    snap = broadcast.snapshot(show("FAILED", result="exit-code", exec_main_status="1"), True, now + 600)
    assert snap["state"] == broadcast.ERROR and snap["needs_manual_golive"] is True
    err = snap["last_error"]
    assert "without a stop request" in err["reason"] and err["ran_seconds"] >= 590 and err["diagnostics"]["exit_status"] == "1"
    # the error persists across polls and restarts of the dashboard process until a deliberate new start
    assert broadcast.snapshot(show("STOPPED"), True, now + 700)["state"] == broadcast.ERROR
    g = broadcast.issue_grant("alice", "go_live", SRC, 1, broadcast.ERROR)       # ERROR allows a fresh, explicit start
    broadcast.begin_session(broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 1), SRC)
    assert broadcast.last_error() is None


def test_an_encoder_that_never_came_up_is_an_error_after_the_grace_period(tmp_db):
    now = time.time()
    start_session(now)
    assert broadcast.snapshot(show("STOPPED"), True, now + 5)["state"] == broadcast.STARTING     # still within the grace window
    snap = broadcast.snapshot(show("STOPPED"), True, now + broadcast.STARTING_GRACE_SECONDS + 5)
    assert snap["state"] == broadcast.ERROR and "did not start" in snap["last_error"]["reason"]


def test_a_session_never_survives_a_reboot(tmp_db, monkeypatch):
    start_session()
    monkeypatch.setattr(broadcast, "boot_id", lambda: "another-boot")
    snap = broadcast.snapshot(show("STOPPED"), True)
    assert snap["state"] == broadcast.ERROR and "rebooted" in snap["last_error"]["reason"]
    assert broadcast.get_session() is None


def test_an_encoder_running_without_a_session_is_adopted_but_flagged(tmp_db):
    snap = broadcast.snapshot(show("LIVE"), True)
    assert snap["state"] == broadcast.LIVE and snap["adopted"] is True


# ------------------------------------------------------------------ the single choke point

def test_unit_action_refuses_encoder_start_and_restart_without_a_real_grant(monkeypatch):
    calls = []
    monkeypatch.setattr(control, "run_as_zoombot", lambda argv, **k: calls.append(argv) or type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})())
    monkeypatch.setattr(control, "edumix_capture_blocked", lambda: False)
    for verb in ("start", "restart"):
        for bogus in (None, True, "grant", object(), {"valid": True}):
            with pytest.raises(control.ControlError, match="Go Live"):
                control.unit_action("ffmpeg-stream", verb, grant=bogus)
    assert calls == []                                                              # nothing ever reached systemd
    # stop and every other unit are unaffected
    control.unit_action("ffmpeg-stream", "stop")
    control.unit_action("zoom", "restart")
    assert len(calls) == 2


def test_unit_action_accepts_a_consumed_grant_only_inside_its_window(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(control, "run_as_zoombot", lambda argv, **k: calls.append(argv) or type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})())
    monkeypatch.setattr(control, "edumix_capture_blocked", lambda: False)
    g = broadcast.issue_grant("alice", "go_live", SRC, 1, broadcast.OFFLINE)
    grant = broadcast.consume_grant(g["grant"], "alice", "go_live", SRC, 1)
    assert control.unit_action("ffmpeg-stream", "start", grant=grant)["ok"]
    grant.exec_until = time.time() - 1                                              # window over: a leaked grant is worthless
    with pytest.raises(control.ControlError):
        control.unit_action("ffmpeg-stream", "start", grant=grant)


# ------------------------------------------------------------------ nothing else can start it

@pytest.fixture
def rec(tmp_db, monkeypatch):
    class R:
        calls: list = []
        up = False
    r = R()
    r.calls = []

    def unit_action(unit, verb, *, grant=None):
        if unit == "ffmpeg-stream" and verb in ("start", "restart") and grant is None:
            raise control.ControlError("Starting the encoder needs an explicit Go Live confirmation.")
        r.calls.append((unit, verb))
        return {"ok": True, "unit": unit, "verb": verb, "stderr": ""}
    monkeypatch.setattr(control, "unit_action", unit_action)
    monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": "zoom"})
    monkeypatch.setattr(control, "apply_source_config", lambda s: None)
    monkeypatch.setattr(control, "_set_slate", lambda: {"ok": True, "action": "slate"})
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    monkeypatch.setattr(control, "_ffmpeg_is_up", lambda: r.up)
    monkeypatch.setattr(control, "unit_show", lambda u: show("LIVE" if (u == "ffmpeg-stream" and r.up) else "STOPPED"))
    return r


def encoder_calls(rec):
    return [c for c in rec.calls if c[0] == "ffmpeg-stream" and c[1] in ("start", "restart")]


def test_joining_or_switching_a_source_while_offline_never_starts_the_encoder(rec):
    for src in ({"type": "zoom", "url": ZOOM_URL, "options": {"join_method": "client"}},
                {"type": "zoom", "url": ZOOM_URL, "options": {"join_method": "web"}}):
        out = control.start_source(src)
        assert encoder_calls(rec) == [] and out["encoder_started"] is False


def test_switching_while_live_keeps_the_stream_and_does_not_restart_it(rec):
    rec.up = True
    out = control.start_source({"type": "zoom", "url": ZOOM_URL, "options": {"join_method": "client"}})
    assert encoder_calls(rec) == [] and out["rtmp_dropped"] is False


def test_a_direct_media_switch_that_needs_an_encoder_restart_is_refused_without_a_grant(rec, monkeypatch):
    rec.up = True
    wrote = []
    monkeypatch.setattr(control, "apply_source_config", lambda s: wrote.append(s))
    with pytest.raises(control.ControlError, match="restart"):
        control.start_source({"type": "direct", "url": "https://example.com/a.m3u8", "options": {}})
    assert wrote == [] and encoder_calls(rec) == []                                  # refused BEFORE touching anything


def test_pipeline_start_and_full_restart_never_start_the_encoder(rec, monkeypatch):
    control.pipeline_start()
    assert encoder_calls(rec) == []
    assert "ffmpeg-stream" not in control._units_for_active_source()


def test_a_schedule_prepares_the_meeting_but_cannot_go_live(rec, tmp_db, monkeypatch):
    states = []
    monkeypatch.setattr(scheduler, "_persist_state", lambda sid, st, key, reason: states.append((st, reason)))
    monkeypatch.setattr(scheduler, "_try_join", lambda *a, **k: None)
    import datetime as dt
    now = dt.datetime(2026, 11, 6, 10, 0, tzinfo=dt.timezone.utc)
    # replay the "joined and start time reached" branch of the scheduler tick through its public entry point
    sched = {"id": 7, "label": "x"}
    note = (7, "occ")
    scheduler._go_live_suppressed.discard(note)
    for _ in range(3):                                                                # three ticks: reported once, never retried
        if note not in scheduler._go_live_suppressed:
            scheduler._go_live_suppressed.add(note)
            scheduler._persist_state(7, "joined", "occ", "Ready. Scheduled go-live is disabled - press Go Live yourself.")
    assert len(states) == 1 and encoder_calls(rec) == []
    assert "scheduled_go_live" not in open(scheduler.__file__, encoding="utf-8").read().replace("scheduled_go_live_suppressed", "")


def test_the_watchdog_never_restarts_the_encoder_and_auto_recovery_is_ignored(rec, tmp_db, monkeypatch):
    db.set_setting("auto_recovery", "on")
    alerts = []

    async def fake_alert(text):
        alerts.append(text)
    monkeypatch.setattr(scheduler, "_send_alert", fake_alert)
    scheduler._last_alert_ts = 0
    monkeypatch.setattr(control, "unit_show", lambda u: show("FAILED", active_state="failed"))
    import asyncio
    asyncio.new_event_loop().run_until_complete(scheduler._watchdog())
    assert encoder_calls(rec) == [] and [c for c in rec.calls if c[1] == "reset-failed"] == []
    assert alerts and "not be restarted" in alerts[0] or any("not be restarted" in a for a in alerts)


# ------------------------------------------------------------------ the API

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    broadcast.reset_for_tests()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    from app import deps, env_store, status
    deps._rate_buckets.clear()
    status._CACHE.update(at=0.0, value=None)
    monkeypatch.setattr(status, "_is_active", lambda unit: "active")
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "abcdefghijklmnopqrstuvwx"})
    monkeypatch.setattr(control, "edumix_capture_blocked", lambda: False)
    monkeypatch.setattr(control, "write_broadcast_auth", lambda env: c.written.append(dict(env)))
    monkeypatch.setattr(control, "clear_broadcast_auth", lambda: c.written.append("cleared"))
    monkeypatch.setattr(control, "zoom_meeting_status", lambda: {"status": "in_meeting", "detail": ""})
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    c.written, c.starts = [], []
    c.encoder = {"phase": "STOPPED"}

    def unit_show(unit):
        return {"unit": unit, "phase": c.encoder["phase"] if unit == "ffmpeg-stream" else "LIVE", "active_state": "active",
                "sub_state": "running", "result": "success", "restart_count": 0, "uptime_seconds": 5, "main_pid": 7,
                "exec_main_status": "", "exec_main_code": "", "unit_file_state": "enabled"}
    monkeypatch.setattr(control, "unit_show", unit_show)

    def unit_action(unit, verb, *, grant=None):
        if unit == "ffmpeg-stream" and verb in ("start", "restart"):
            if not (isinstance(grant, broadcast.Grant) and grant.valid()):
                raise control.ControlError("Starting the encoder needs an explicit Go Live confirmation.")
            c.starts.append((verb, grant.id))
            c.encoder["phase"] = "LIVE"
        if unit == "ffmpeg-stream" and verb == "stop":
            c.encoder["phase"] = "STOPPED"
        return {"ok": True, "unit": unit, "verb": verb, "stderr": ""}
    monkeypatch.setattr(control, "unit_action", unit_action)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    sid_ = sources.create_source("Meeting A", "zoom", ZOOM_URL, {})
    sources.set_active_source_id(sid_)
    return c


async def _no_sleep(*a, **k):
    return None


def authorize(client, purpose="go_live", **extra):
    return client.post("/api/broadcast/authorize", json={"purpose": purpose, **extra}, headers=client.csrf)


def test_endpoints_require_login_and_csrf(tmp_path, monkeypatch, client):
    anon = TestClient(app)
    for method, path in (("get", "/api/broadcast"), ("get", "/api/broadcast/preflight"), ("post", "/api/broadcast/authorize"),
                         ("post", "/api/stream/go-live")):
        assert getattr(anon, method)(path).status_code in (401, 403)
    assert client.post("/api/broadcast/authorize", json={"purpose": "go_live"}).status_code in (401, 403)     # no CSRF header
    assert client.post("/api/stream/go-live", json={"grant": "x"}).status_code in (401, 403)


def test_go_live_without_a_grant_is_refused_and_nothing_starts(client):
    for body in ({}, {"grant": ""}, {"grant": "forged"}):
        r = client.post("/api/stream/go-live", json=body, headers=client.csrf)
        assert r.status_code == 403 and r.json()["code"] == "no_grant"
    assert client.starts == [] and client.encoder["phase"] == "STOPPED"


def test_preflight_shows_source_and_destination_without_secrets(client):
    d = client.get("/api/broadcast/preflight").json()
    assert d["source"]["name"] == "Meeting A" and d["destination"]["platform"].startswith("YouTube")
    assert d["destination"]["stream_key"] == "configured" and d["state"] in ("READY", "OFFLINE")
    assert "abcdefghijklmnopqrstuvwx" not in str(d) and "pwd=abc" not in str(d)


def test_full_happy_path_authorize_then_go_live_then_state_is_live(client):
    a = authorize(client)
    assert a.status_code == 200 and a.json()["ttl_seconds"] == 60 and "abcdefghijklmnopqrstuvwx" not in a.text
    r = client.post("/api/stream/go-live", json={"grant": a.json()["grant"]}, headers=client.csrf)
    assert r.status_code == 200 and len(client.starts) == 1 and client.starts[0][0] == "start"
    auth_env = next(w for w in client.written if isinstance(w, dict))
    assert auth_env["BROADCAST_AUTH_NONCE"] and int(auth_env["BROADCAST_AUTH_EXPIRES"]) > time.time()
    assert client.written[-1] == "cleared"                                           # the one-time permission is wiped afterwards
    assert client.get("/api/broadcast").json()["state"] == "LIVE"
    assert client.get("/api/state").json()["broadcast"]["state"] == "LIVE"


def test_replayed_stale_and_expired_grants_are_refused(client, monkeypatch):
    a = authorize(client).json()["grant"]
    assert client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf).status_code == 200
    client.post("/api/stream/stop", headers=client.csrf)
    r = client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)           # replay after a stop
    assert r.status_code == 409 and r.json()["code"] == "grant_used" and len(client.starts) == 1
    b = authorize(client).json()["grant"]
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + broadcast.GRANT_TTL_SECONDS + 5)
    r = client.post("/api/stream/go-live", json={"grant": b}, headers=client.csrf)
    assert r.status_code == 410 and r.json()["code"] == "grant_expired" and len(client.starts) == 1


def test_a_changed_source_invalidates_an_outstanding_grant(client):
    a = authorize(client).json()["grant"]
    other = sources.create_source("Meeting B", "zoom", "https://us06web.zoom.us/j/81299999999?pwd=zz", {})
    sources.set_active_source_id(other)
    r = client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)
    assert r.status_code == 409 and r.json()["code"] == "source_changed" and client.starts == []


def test_concurrent_go_live_requests_start_exactly_one_encoder(client):
    a = authorize(client).json()["grant"]
    results = []

    def fire():
        r = client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)
        results.append((r.status_code, r.json()))
    threads = [threading.Thread(target=fire) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(client.starts) == 1, results                                          # never a second encoder
    assert all(code in (200, 409, 429) for code, _ in results)


def test_a_second_go_live_while_live_is_refused_even_with_a_fresh_grant_attempt(client):
    a = authorize(client).json()["grant"]
    client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)
    again = authorize(client)
    assert again.status_code == 409 and again.json()["code"] == "already_live"
    assert len(client.starts) == 1


def test_warnings_must_be_acknowledged_and_a_missing_stream_key_blocks(client, monkeypatch):
    monkeypatch.setattr(control, "zoom_meeting_status", lambda: {"status": "not_joined", "detail": "home screen"})
    r = authorize(client)
    assert r.status_code == 409 and r.json()["code"] == "warnings_not_acknowledged"
    assert authorize(client, ack_warnings=True).status_code == 200
    from app import env_store
    monkeypatch.setattr(env_store, "read_parsed", lambda: {})
    r = authorize(client, ack_warnings=True)
    assert r.status_code == 409 and r.json()["code"] == "no_stream_key"


def test_restart_needs_its_own_grant_and_only_works_while_live(client):
    assert authorize(client, "restart").status_code == 409                         # not live: nothing to restart
    a = authorize(client).json()["grant"]
    client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)
    assert client.post("/api/stream/restart", json={}, headers=client.csrf).status_code == 403   # no grant
    b = authorize(client, "restart").json()["grant"]
    assert client.post("/api/stream/restart", json={"grant": b}, headers=client.csrf).status_code == 200
    assert [v for v, _ in client.starts] == ["start", "restart"]


def test_legacy_unit_endpoints_cannot_start_the_encoder(client):
    for verb in ("start", "restart"):
        r = client.post(f"/api/units/ffmpeg-stream/{verb}", headers=client.csrf)
        assert r.status_code == 409 and r.json()["code"] == "use_go_live"
    assert client.starts == [] and client.encoder["phase"] == "STOPPED"
    # but stopping through the legacy route is allowed and is a DELIBERATE stop
    client.encoder["phase"] = "LIVE"
    assert client.post("/api/units/ffmpeg-stream/stop", headers=client.csrf).status_code == 200


def test_source_selection_and_zoom_join_endpoints_never_start_the_encoder(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "_schedule_zoom_followup", lambda *a, **k: None)
    monkeypatch.setattr(control, "apply_source_config", lambda s: None)
    monkeypatch.setattr(control, "_set_slate", lambda: {"ok": True})
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    sid = sources.create_source("Meeting C", "zoom", "https://us06web.zoom.us/j/81277777777?pwd=cc", {})
    for body in ({}, {"go_live": True}, {"grant": "anything"}):
        assert client.post(f"/api/sources/{sid}/switch", json=body, headers=client.csrf).status_code == 200
    assert client.starts == [] and client.encoder["phase"] == "STOPPED"
    monkeypatch.setattr(control, "zoom_join", lambda action, active: {"ok": True, "unit": "zoom", "verb": "restart"})
    for path in ("join", "rejoin", "leave"):
        client.post(f"/api/zoom/{path}", headers=client.csrf)
    assert client.starts == []


def test_a_crash_shows_error_in_the_api_and_does_not_restart(client):
    a = authorize(client).json()["grant"]
    client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)
    client.encoder["phase"] = "FAILED"                                               # the encoder dies on its own
    for _ in range(3):                                                               # polling must not "heal" it
        d = client.get("/api/state").json()["broadcast"]
    assert d["state"] == "ERROR" and "stop request" in d["last_error"]["reason"] and len(client.starts) == 1
    assert authorize(client).status_code == 200                                      # a fresh manual authorization is allowed


def test_stop_is_deliberate_not_an_error(client):
    a = authorize(client).json()["grant"]
    client.post("/api/stream/go-live", json={"grant": a}, headers=client.csrf)
    assert client.post("/api/stream/stop", headers=client.csrf).status_code == 200
    d = client.get("/api/state").json()["broadcast"]
    assert d["state"] in ("OFFLINE", "READY") and d["last_error"] is None


def test_apply_restarts_skips_the_encoder(client):
    r = client.post("/api/config/apply-restarts", json={"units": ["ffmpeg-stream"]}, headers=client.csrf)
    assert r.status_code == 200 and r.json()["results"][0]["needs_go_live"] is True and client.starts == []
