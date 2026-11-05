"""EduMix / MJPhysics source: URL validation, page-state classification,
reconnect policy, capture gate, source integration, API and supervisor.

Everything here is mocked - no real account, no real Chrome, no live class.
What these tests can NOT show (needs a real authenticated live session) is
listed in docs/edumix.md under "Not verified"."""
import asyncio
import time

import pytest
from starlette.testclient import TestClient

from app import cdp, config, control, db, edumix, edumix_api, security, sources
from app.env_store import ValidationError
from app.main import app

CLASS = "https://mjphysics.lk/class/0123456789abcdef01234567"


# ------------------------------------------------------------------ URL validation

@pytest.mark.parametrize("raw,expected", [
    (CLASS, CLASS),
    ("mjphysics.lk/class/abc", "https://mjphysics.lk/class/abc"),
    ("  https://MJPhysics.LK/live-class  ", "https://mjphysics.lk/live-class"),
    ("https://www.mjphysics.lk/", "https://www.mjphysics.lk/"),
    ("https://app.mjphysics.lk/class/1?x=1#frag", "https://app.mjphysics.lk/class/1?x=1"),
    ("https://mjphysics.lk:443/class/1", "https://mjphysics.lk/class/1"),
    ("https://mjphysics.lk", "https://mjphysics.lk/"),
])
def test_valid_urls(raw, expected):
    assert edumix.validate_class_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "http://mjphysics.lk/class/1", "ftp://mjphysics.lk/", "javascript:alert(1)",
    "file:///etc/passwd", "https://evil.com/", "https://evil.com/mjphysics.lk",
    "https://mjphysics.lk.evil.com/", "https://evilmjphysics.lk/", "https://notmjphysics.lk/",
    "https://mjphysics.lk@evil.com/", "https://user:pw@mjphysics.lk/", "https://mjphysics.lk:8443/",
    "https://mjphysics.lk/a b", "https://mjphysics.lk/a\nb", "https://mjphysics.lk\\@evil.com/",
    "https://mjphysics.lk/\\evil", "https://127.0.0.1/", "-https://mjphysics.lk/",
    "https://mjphysics.lk/" + "a" * 2100,
])
def test_invalid_urls(raw):
    with pytest.raises(edumix.EduMixError):
        edumix.validate_class_url(raw)


# ------------------------------------------------------------------ classify

def snap(**kw):
    """A signed-in page by default (the site's Logout control is the evidence)."""
    markers = {"logout": True}
    markers.update(kw.pop("markers", {}))
    base = {"url": "https://mjphysics.lk/dashboard", "ready": "complete", "online": True,
            "login_form": False, "otp_form": False, "inputs": 0, "markers": markers, "videos": [], "frames": []}
    base.update(kw)
    return base


def video(**kw):
    v = {"paused": False, "ended": False, "readyState": 4, "networkState": 1, "error": 0, "time": 5,
         "w": 1280, "h": 720, "protected": False, "muted": False, "volume": 1}
    v.update(kw)
    return v


@pytest.mark.parametrize("s,state", [
    (None, "browser_down"),
    (snap(online=False), "network_down"),
    (snap(ready="loading"), "loading"),
    (snap(login_form=True), "login_required"),
    (snap(url="https://mjphysics.lk/login"), "login_required"),
    (snap(otp_form=True), "awaiting_otp"),
    (snap(otp_form=True, login_form=True), "awaiting_otp"),
    (snap(), "authenticated"),
    (snap(url="https://mjphysics.lk/class/abc"), "class_open"),
    (snap(url="https://mjphysics.lk/live-class", markers={"no_live": True}), "no_live"),
    (snap(url="https://mjphysics.lk/live-class"), "no_live"),
    (snap(markers={"session_expired": True}), "access_expired"),                       # signed in: class access ended
    (snap(markers={"session_expired": True, "logout": False}, tl=500), "session_expired"),   # signed out for real
    (snap(markers={"error_text": True}), "error"),
    # a single-page app that has not rendered yet (no Logout, no inputs, ~no text) is "loading", not "unknown"
    (snap(url="https://mjphysics.lk/", tl=0, markers={"logout": False}), "loading"),
    (snap(url="https://mjphysics.lk/", tl=12, markers={"logout": False}), "loading"),
    (snap(url="https://mjphysics.lk/", tl=400, markers={"logout": False}), "unknown"),
    # the REAL mjphysics.lk login (observed): /login, one phone field (id=phone), a Login button
    (snap(url="https://mjphysics.lk/login", login_form=True, inputs=1, tl=48, markers={"logout": False, "login_text": True}), "login_required"),
    # positive evidence needed: an unrecognised page without Logout is never "signed in"
    (snap(markers={"logout": False}), "unknown"),
    (snap(url="https://mjphysics.lk/", markers={"logout": False}), "unknown"),
    # login/OTP detected from visible text + an input even when the field attributes give nothing away
    (snap(url="https://mjphysics.lk/", markers={"logout": False, "login_text": True}, inputs=1), "login_required"),
    (snap(url="https://mjphysics.lk/", markers={"logout": False, "login_text": True, "otp_text": True}, inputs=1), "awaiting_otp"),
    (snap(markers={"login_text": True, "otp_text": True}, inputs=1), "authenticated"),   # signed-in page that merely mentions them
    # phone field + a "Send OTP" button is still the LOGIN step (found with a real Chrome)
    (snap(url="https://mjphysics.lk/login", login_form=True, inputs=1, markers={"logout": False, "login_text": True}), "login_required"),
    (snap(url="https://mjphysics.lk/", login_form=True, inputs=1, markers={"logout": False, "login_text": True, "otp_text": False}), "login_required"),
    (snap(url="https://mjphysics.lk/", inputs=1, markers={"logout": False, "otp_text": True}), "awaiting_otp"),
    (snap(markers={"drm_text": True}), "protected"),
    (snap(videos=[video()]), "playing"),
    (snap(videos=[video(paused=True)]), "paused"),
    (snap(videos=[video(readyState=2)]), "buffering"),
    (snap(videos=[video(ended=True, paused=True)]), "ended"),
    (snap(videos=[video(error=4)]), "player_error"),
    (snap(videos=[video(protected=True)]), "protected"),
    (snap(videos=[video(paused=True, readyState=0, networkState=2)]), "player_loading"),
    (snap(url="https://mjphysics.lk/class/abc", frames=["player.vimeo.com"]), "player_loading"),
])
def test_classify(s, state):
    assert edumix.classify(s) == state


def test_protected_playback_blocks_capture_even_if_the_flag_were_on(monkeypatch):
    assert edumix.capture_status("protected")["enabled"] is False
    monkeypatch.setattr(config, "EDUMIX_CAPTURE_ENABLED", True)
    assert edumix.capture_status("playing")["enabled"] is True          # the flag alone would allow it...
    st = edumix.capture_status("protected")
    assert st["enabled"] is False and "never" in st["reason"]           # ...but protected playback never does


def test_status_reports_capture_disabled_for_protected_page(monkeypatch):
    async def ev(expr, user_gesture=False):
        import json
        return {"value": json.dumps(snap(videos=[video(protected=True)]))}
    monkeypatch.setattr(cdp, "evaluate", ev)
    st = run(edumix.status())
    assert st["state"] == "protected" and st["capture"]["enabled"] is False and st["needs_human"]


def test_hard_failures_beat_soft_ones():
    # offline wins over a "playing" video; protected wins over playing
    assert edumix.classify(snap(online=False, videos=[video()])) == "network_down"
    assert edumix.classify(snap(videos=[video(protected=True)])) == "protected"


def test_every_state_is_described_and_human_flags():
    for state in edumix.STATES:
        d = edumix.describe(state)
        assert d["label"] and d["level"] in ("ok", "info", "warn", "bad", "idle")
    assert edumix.describe("session_expired")["needs_human"]
    assert edumix.describe("protected")["needs_human"]
    assert not edumix.describe("playing")["needs_human"]


def test_probe_js_has_no_control_characters_and_keeps_word_boundaries():
    """A literal backspace (from an unescaped word-boundary escape) once silently disabled the
    Logout marker: every signed-in page then read as 'unknown'. Only a real browser noticed -
    keep this guard."""
    assert not [c for c in edumix.JS_PROBE if ord(c) < 32 and c not in "\n\t"]
    assert r"\blog ?out\b" in edumix.JS_PROBE and r"\bresend\b" in edumix.JS_PROBE
    # bare "otp" must NOT mark the OTP step: a login page's "Send OTP" button would read as verification
    assert r"\botp\b" not in edumix.JS_PROBE


def test_probe_js_never_reads_secrets():
    js = edumix.JS_PROBE
    for forbidden in ("cookie", "localStorage", "sessionStorage", ".value", "password"):
        assert forbidden not in js


# ------------------------------------------------------------------ backoff / reconnector

def test_backoff_exponential_capped_with_jitter():
    mid = [edumix.backoff_delay(n, base=2, cap=60, rand=0.5) for n in range(1, 8)]
    assert mid == [2, 4, 8, 16, 32, 60, 60]
    assert edumix.backoff_delay(3, base=2, cap=60, rand=0.0) == pytest.approx(8 * 0.8)
    assert edumix.backoff_delay(3, base=2, cap=60, rand=1.0) == pytest.approx(8 * 1.2)


def test_reconnector_retries_with_growing_gaps_then_resets():
    r = edumix.Reconnector()
    now = 1000.0
    assert r.step("network_down", now, rand=0.5) == "reload"
    assert r.step("network_down", now + 1, rand=0.5) is None          # still backing off
    assert r.step("network_down", now + 2.1, rand=0.5) == "reload"    # attempt 2 after 2s
    assert r.step("network_down", now + 2.1 + 3.9, rand=0.5) is None  # attempt 3 needs 4s
    assert r.step("network_down", now + 2.1 + 4.1, rand=0.5) == "reload"
    assert r.attempt == 3
    assert r.step("playing", now + 100) is None
    assert r.attempt == 0


def test_reconnector_restarts_browser_after_waiting_for_systemd():
    r = edumix.Reconnector(restart_after_attempts=3)
    t, acts = 0.0, []
    for _ in range(12):
        t += 120
        acts.append(r.step("browser_down", t, rand=0.5))
    assert acts[:3] == [None, None, "restart_browser"]


def test_reconnector_never_touches_human_states_and_gives_up():
    r = edumix.Reconnector(max_attempts=2)
    for state in edumix.NEEDS_HUMAN:
        assert r.step(state, 10_000) is None
    assert r.attempt == 0
    t = 0.0
    out = []
    for _ in range(5):
        t += 500
        out.append(r.step("player_error", t, rand=0.5))
    assert out.count("reload") == 2 and r.snapshot(t)["gave_up"]


@pytest.mark.parametrize("state,cur,tgt,auto,done,want", [
    ("authenticated", "/dashboard", "/class/abc", True, False, True),
    ("no_live", "/live-class", "/class/abc", True, False, True),
    ("authenticated", "/dashboard", "/class/abc", False, False, False),   # option off
    ("authenticated", "/dashboard", "/class/abc", True, True, False),     # already done this episode
    ("authenticated", "/class/abc", "/class/abc", True, False, False),    # already there
    ("authenticated", "/class/abc/", "/class/abc", True, False, False),
    ("login_required", "/login", "/class/abc", True, False, False),       # not signed in yet
    ("playing", "/class/x", "/class/abc", True, False, False),            # never yank a playing page
    ("authenticated", "/dashboard", "/", True, False, False),
])
def test_should_auto_navigate(state, cur, tgt, auto, done, want):
    assert edumix.should_auto_navigate(state, cur, tgt, auto, done) is want


# ------------------------------------------------------------------ redaction

def test_redact_masks_phone_and_credentials():
    text = "login phone +94700000000 otp=482913 password: hunter2 cookie=abc123 ok"
    out = edumix.redact(text)
    for leaked in ("94700000000", "482913", "hunter2", "abc123"):
        assert leaked not in out
    assert "ok" in out


# ------------------------------------------------------------------ capture gate

def test_capture_disabled_by_default_and_gate():
    assert config.EDUMIX_CAPTURE_ENABLED is False
    assert not edumix.capture_status()["enabled"] and edumix.capture_status()["reason"]
    assert edumix.capture_blocked("edumix")
    assert not edumix.capture_blocked("zoom") and not edumix.capture_blocked("webpage") and not edumix.capture_blocked(None)


class _Proc:
    returncode = 0
    stderr = b""
    stdout = b""


def test_encoder_cannot_start_for_edumix(monkeypatch):
    calls = []
    monkeypatch.setattr(control, "run_as_zoombot", lambda *a, **k: calls.append(a) or _Proc())
    monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": "edumix"})
    for verb in ("start", "restart"):
        with pytest.raises(control.ControlError, match="EduMix"):
            control.unit_action("ffmpeg-stream", verb)
    assert calls == []
    # stopping and unrelated units are unaffected
    assert control.unit_action("ffmpeg-stream", "stop")["ok"]
    assert control.unit_action("browser-source", "restart")["ok"]


def test_encoder_still_starts_for_other_sources(monkeypatch):
    """...but only with an authorized grant (Go Live): without one nothing starts, whatever the source."""
    from app import broadcast
    monkeypatch.setattr(control, "run_as_zoombot", lambda *a, **k: _Proc())
    for st in ("zoom", "webpage", "direct", ""):
        monkeypatch.setattr(control, "read_current_source", lambda st=st: {"SOURCE_TYPE": st})
        with pytest.raises(control.ControlError, match="Go Live"):
            control.unit_action("ffmpeg-stream", "start")
        broadcast.reset_for_tests()
        g = broadcast.issue_grant("op", "go_live", None, 1, broadcast.OFFLINE)
        grant = broadcast.consume_grant(g["grant"], "op", "go_live", None, 1)
        assert control.unit_action("ffmpeg-stream", "start", grant=grant)["ok"]


# ------------------------------------------------------------------ sources + control integration

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    yield


def test_create_edumix_source(tmp_db):
    sid = sources.create_source("EduMix Live", "edumix", CLASS + "#x", {})
    s = sources.get_source(sid)
    assert s["type"] == "edumix" and s["url"] == CLASS
    assert s["options"] == {"auto_navigate": True, "rights_confirmed": False}
    assert sources.public_view(s)["type"] == "edumix"


@pytest.mark.parametrize("bad", ["https://evil.com/x", "http://mjphysics.lk/", "https://mjphysics.lk@evil.com/"])
def test_edumix_source_rejects_other_hosts(tmp_db, bad):
    with pytest.raises(ValidationError):
        sources.create_source("X", "edumix", bad, {})


def test_detect_type_and_other_types_unchanged(tmp_db):
    assert sources.detect_type(CLASS) == "edumix"
    assert sources.detect_type("https://us06web.zoom.us/j/81234567890") == "zoom"
    assert sources.detect_type("https://example.com/page") == "webpage"
    assert sources.detect_type("https://example.com/x.m3u8") == "direct"
    # a webpage source may not be pointed at a non-edumix host via the edumix type, and vice versa is fine
    sid = sources.create_source("Web", "webpage", "https://example.com/", {})
    assert sources.get_source(sid)["type"] == "webpage"


def test_env_lines_use_isolated_profile(tmp_db):
    sid = sources.create_source("EduMix Live", "edumix", CLASS, {})
    vals = control._source_env_lines(sources.get_source(sid))
    assert vals["SOURCE_TYPE"] == "edumix"
    assert vals["EDUMIX_URL"] == CLASS
    assert vals["ACCOUNT_PROFILE_ID"] == config.EDUMIX_PROFILE_ID == "edumix"
    assert vals["WEBPAGE_URL"] == ""
    assert "EDUMIX_URL" in control.SOURCE_ENV_KEYS


def test_env_lines_revalidate_url(tmp_db):
    with pytest.raises(edumix.EduMixError):
        control._source_env_lines({"type": "edumix", "url": "https://evil.com/", "options": {}, "account_id": None})


def test_producer_unit_is_browser_source():
    assert control.producer_unit_for({"type": "edumix"}) == "browser-source"
    assert control.producer_unit_for({"type": "webpage"}) == "browser-source"
    assert control.producer_unit_for({"type": "direct"}) is None
    assert control.producer_unit_for({"type": "zoom", "options": {"join_method": "client"}}) == "zoom"


@pytest.fixture
def fake_units(monkeypatch):
    calls = []
    monkeypatch.setattr(control, "unit_action", lambda unit, verb: calls.append((unit, verb)) or {"ok": True})
    monkeypatch.setattr(control, "apply_source_config", lambda s: None)
    monkeypatch.setattr(control, "_set_slate", lambda: {"ok": True})
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    return calls


def test_start_source_edumix_is_view_only(tmp_db, monkeypatch, fake_units):
    monkeypatch.setattr(control, "_ffmpeg_is_up", lambda: False)
    sid = sources.create_source("EduMix Live", "edumix", CLASS, {})
    out = control.start_source(sources.get_source(sid))
    assert ("browser-source", "restart") in fake_units
    assert ("ffmpeg-stream", "start") not in fake_units
    assert ("zoom", "stop") in fake_units
    assert out["rtmp_dropped"] is False


def test_start_source_edumix_refused_while_live(tmp_db, monkeypatch, fake_units):
    monkeypatch.setattr(control, "_ffmpeg_is_up", lambda: True)
    sid = sources.create_source("EduMix Live", "edumix", CLASS, {})
    with pytest.raises(control.ControlError, match="live"):
        control.start_source(sources.get_source(sid))
    assert fake_units == []          # nothing was touched


def test_start_source_for_other_sources_prepares_them_but_never_starts_the_encoder(tmp_db, monkeypatch, fake_units):
    """Regression: the edumix branch must not change other sources - and no source start may put anything on air."""
    monkeypatch.setattr(control, "_ffmpeg_is_up", lambda: False)
    monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": "zoom"})
    sid = sources.create_source("W", "webpage", "https://example.com/", {})
    control.start_source(sources.get_source(sid))
    assert ("browser-source", "restart") in fake_units and ("ffmpeg-stream", "start") not in fake_units


def test_pipeline_start_skips_encoder_for_edumix(monkeypatch):
    calls = []
    monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": "edumix"})
    monkeypatch.setattr(control, "unit_action", lambda u, v: calls.append(u) or {"ok": True})
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    control.pipeline_start()
    assert "browser-source" in calls and "ffmpeg-stream" not in calls and "zoom" not in calls
    assert "ffmpeg-stream" not in control._units_for_active_source()


# ------------------------------------------------------------------ API

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    return c


@pytest.fixture
def anon(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "anon.db")
    db.init_db()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    edumix.invalidate()
    yield
    edumix.invalidate()


@pytest.fixture(autouse=True)
def _fresh_supervisor():
    from app import deps
    edumix_api.supervisor.__init__()
    deps._rate_buckets.clear()       # the limiter is process-global; keep tests independent
    yield


def test_api_requires_auth(anon):
    assert anon.get("/api/edumix/status").status_code == 401
    assert anon.post("/api/edumix/open", json={"url": CLASS}).status_code in (401, 403)
    assert anon.get("/edumix", follow_redirects=False).status_code in (302, 303, 307)


def test_api_post_requires_csrf(client):
    assert client.post("/api/edumix/open", json={"url": CLASS}).status_code == 403
    assert client.post("/api/edumix/login", json={}).status_code == 403
    assert client.post("/api/edumix/stop", json={}).status_code == 403


def test_status_when_nothing_open(client):
    r = client.get("/api/edumix/status")
    j = r.json()
    assert r.status_code == 200 and j["state"] == "stopped" and j["active"] is False
    assert j["capture"]["enabled"] is False and j["source"] is None


def test_open_rejects_bad_urls_without_touching_anything(client, monkeypatch):
    monkeypatch.setattr(control, "start_source", lambda s: pytest.fail("must not start"))
    for bad in ("https://evil.com/", "http://mjphysics.lk/", "", "https://mjphysics.lk@evil.com/"):
        r = client.post("/api/edumix/open", json={"url": bad}, headers=client.csrf)
        assert r.status_code == 400 and "error" in r.json()
    assert edumix_api.get_edumix_source() is None


def test_open_creates_source_starts_it_and_audits_path_only(client, monkeypatch):
    started = []
    monkeypatch.setattr(control, "start_source", lambda s: started.append(s) or
                        {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    url = CLASS + "?token=SECRETTOKEN"
    r = client.post("/api/edumix/open", json={"url": url, "auto_navigate": True}, headers=client.csrf)
    assert r.status_code == 200 and r.json()["ok"]
    assert len(started) == 1 and started[0]["type"] == "edumix"
    assert sources.get_active_source()["type"] == "edumix"
    with db.get_conn() as conn:
        rows = [dict(x) for x in conn.execute("SELECT action, detail FROM audit_log WHERE action='edumix_open'")]
    assert rows and all("SECRETTOKEN" not in (x["detail"] or "") for x in rows)
    # the public view the page gets masks nothing sensitive but never needs the query for display
    j = client.get("/api/edumix/status").json()
    assert j["source"]["path"] == "/class/0123456789abcdef01234567"


def test_open_twice_reuses_the_single_source(client, monkeypatch):
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)
    client.post("/api/edumix/open", json={"url": "https://mjphysics.lk/class/other"}, headers=client.csrf)
    assert len([s for s in sources.list_sources() if s["type"] == "edumix"]) == 1
    assert edumix_api.get_edumix_source()["url"] == "https://mjphysics.lk/class/other"


def test_open_hot_navigates_when_browser_is_up(client, monkeypatch):
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)

    async def probe(): return {"connected": True}
    navigated = []
    async def nav(url): navigated.append(url)
    monkeypatch.setattr(cdp, "probe", probe)
    monkeypatch.setattr(cdp, "navigate", nav)
    monkeypatch.setattr(control, "apply_source_config", lambda s: None)
    monkeypatch.setattr(control, "start_source", lambda s: pytest.fail("session must be kept - no restart"))
    r = client.post("/api/edumix/open", json={"url": "https://mjphysics.lk/class/two"}, headers=client.csrf)
    assert r.status_code == 200 and r.json()["hot_swapped"] is True
    assert navigated == ["https://mjphysics.lk/class/two"]


def test_open_reports_start_failure(client, monkeypatch):
    def boom(s): raise control.ControlError("stream is live")
    monkeypatch.setattr(control, "start_source", boom)
    r = client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)
    assert r.status_code == 409 and "live" in r.json()["error"]


def test_login_needs_active_source_then_navigates_to_site_entry(client, monkeypatch):
    assert client.post("/api/edumix/login", json={}, headers=client.csrf).status_code == 409
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)
    went = []
    async def nav(url): went.append(url)
    monkeypatch.setattr(cdp, "navigate", nav)
    assert client.post("/api/edumix/login", json={}, headers=client.csrf).status_code == 200
    assert went == ["https://mjphysics.lk/"]


def test_reconnect_navigates_or_restarts_browser(client, monkeypatch):
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)
    async def probe_ok(): return {}
    async def nav(url): pass
    monkeypatch.setattr(cdp, "probe", probe_ok); monkeypatch.setattr(cdp, "navigate", nav)
    assert client.post("/api/edumix/reconnect", json={}, headers=client.csrf).json()["mode"] == "navigate"

    async def probe_down(): raise cdp.CDPError("down")
    units = []
    monkeypatch.setattr(cdp, "probe", probe_down)
    monkeypatch.setattr(control, "unit_action", lambda u, v: units.append((u, v)) or {"ok": True})
    assert client.post("/api/edumix/reconnect", json={}, headers=client.csrf).json()["mode"] == "restart_browser"
    assert units == [("browser-source", "reset-failed"), ("browser-source", "restart")]   # clears any start-limit first


def test_stop_pauses_supervisor_and_stops_only_the_browser(client, monkeypatch):
    units = []
    monkeypatch.setattr(control, "unit_action", lambda u, v: units.append((u, v)) or {"ok": True})
    assert client.post("/api/edumix/stop", json={}, headers=client.csrf).status_code == 200
    assert units == [("browser-source", "stop")] and edumix_api.supervisor.paused


def test_fit_validates_mode_and_reports_unapplied(client, monkeypatch):
    assert client.post("/api/edumix/fit", json={"mode": "stretch"}, headers=client.csrf).status_code == 400

    async def ev(expr, user_gesture=False):
        assert "objectFit" in expr
        return {"value": 0}
    monkeypatch.setattr(cdp, "evaluate", ev)
    j = client.post("/api/edumix/fit", json={"mode": "fill"}, headers=client.csrf).json()
    assert j["applied"] is False and "iframe" in j["note"]

    async def ev2(expr, user_gesture=False):
        assert "cover" in expr
        return {"value": 1}
    monkeypatch.setattr(cdp, "evaluate", ev2)
    assert client.post("/api/edumix/fit", json={"mode": "fill"}, headers=client.csrf).json()["applied"] is True


def test_options_update_and_unknown_source(client, monkeypatch):
    assert client.post("/api/edumix/options", json={"auto_navigate": False}, headers=client.csrf).status_code == 404
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)
    r = client.post("/api/edumix/options", json={"auto_navigate": False, "rights_confirmed": True}, headers=client.csrf)
    assert r.json()["source"]["auto_navigate"] is False and r.json()["source"]["rights_confirmed"] is True
    # recording the acknowledgement does NOT enable capture
    assert config.EDUMIX_CAPTURE_ENABLED is False
    assert client.get("/api/edumix/status").json()["capture"]["enabled"] is False


def test_stream_go_live_blocked_for_edumix(client, monkeypatch):
    """The real /api/stream/go-live route: ffmpeg must not start for EduMix."""
    ran = []
    monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": "edumix"})
    monkeypatch.setattr(control, "run_as_zoombot", lambda *a, **k: ran.append(a) or _Proc())
    r = client.post("/api/broadcast/authorize", json={"purpose": "go_live", "ack_warnings": True}, headers=client.csrf)
    assert r.status_code == 403 and r.json()["code"] == "edumix_locked" and "EduMix" in r.json()["error"]
    r = client.post("/api/stream/go-live", json={"grant": "nope"}, headers=client.csrf)       # and no grant can exist anyway
    assert r.status_code == 403
    assert not [c for c in ran if any(v in str(c) for v in ("'start'", "'restart'", " start ", " restart "))]   # read-only 'show' calls only


def test_page_renders_with_oxblood_shell(client):
    r = client.get("/edumix")
    assert r.status_code == 200
    html = r.text
    for needle in ("em-url", "Login / Reauthenticate", "Sign-in helper", "em-stage", "Fit", "Fill", "Reset", "Fullscreen",
                   "css/edumix.css", "js/edumix.js", "capture off", 'href="/edumix"', "mjphysics.lk"):
        assert needle in html, needle
    # the YouTube output card is present but locked
    assert "<fieldset class=\"em-output-form\" disabled" in html
    # no secrets / no inline handlers (CSP is script-src 'self')
    assert "onclick=" not in html and "password" not in html.split("em-secret")[0].lower().replace("type=\"password\"", "")


def test_existing_pages_unaffected(client, monkeypatch):
    from app import env_store
    monkeypatch.setattr(env_store, "read_parsed", lambda: {})                  # /studio reads .env via sudo
    monkeypatch.setattr(control, "run_as_zoombot", lambda *a, **k: _Proc())
    for path in ("/zoom", "/remote", "/studio", "/", "/vnc"):
        assert client.get(path).status_code == 200, path


# ------------------------------------------------------------------ supervisor (mocked page)

def _status(state, path="/dashboard", level="ok"):
    d = edumix.describe(state)
    d.update({"page": {"path": path, "title": ""}, "player": {"present": False}, "capture": edumix.capture_status()})
    return d


@pytest.fixture
def sup(client, monkeypatch):
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)
    edumix_api.supervisor.__init__()
    edumix_api.supervisor.grace_until = 0.0
    log = {"nav": [], "units": [], "state": None, "path": "/dashboard"}

    async def fake_status(rc=None):
        s = _status(log["state"], log["path"])
        if rc is not None:
            s["reconnect"] = rc.snapshot(time.time())
        return s

    async def fake_nav(url): log["nav"].append(url)
    monkeypatch.setattr(edumix, "status", fake_status)
    monkeypatch.setattr(edumix, "navigate_class", fake_nav)
    monkeypatch.setattr(control, "unit_action", lambda u, v: log["units"].append((u, v)) or {"ok": True})
    return log


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_supervisor_navigates_to_class_once_after_login(sup):
    s = edumix_api.supervisor
    sup["state"] = "login_required"; sup["path"] = "/login"
    run(s.tick(100.0)); assert sup["nav"] == []
    sup["state"] = "awaiting_otp"
    run(s.tick(105.0)); assert sup["nav"] == []
    sup["state"] = "authenticated"; sup["path"] = "/dashboard"
    run(s.tick(110.0)); assert sup["nav"] == [CLASS]
    run(s.tick(115.0)); assert sup["nav"] == [CLASS]              # once per episode
    sup["state"] = "session_expired"
    run(s.tick(120.0))
    sup["state"] = "authenticated"; sup["path"] = "/dashboard"
    run(s.tick(125.0)); assert sup["nav"] == [CLASS, CLASS]       # new login episode -> navigates again
    assert db.get_setting(edumix_api.SETTING_LAST_AUTH)           # session-valid timestamp recorded (no secrets)


def test_supervisor_respects_auto_navigate_off(client, sup):
    client.post("/api/edumix/options", json={"auto_navigate": False}, headers=client.csrf)
    sup["state"] = "authenticated"
    run(edumix_api.supervisor.tick(100.0))
    assert sup["nav"] == []


def test_supervisor_reconnects_with_backoff_after_network_drop(sup):
    s = edumix_api.supervisor
    sup["state"] = "network_down"; sup["path"] = "/class/0123456789abcdef01234567"
    run(s.tick(1000.0)); n1 = len(sup["nav"])
    assert n1 == 1                                                # first retry immediately
    run(s.tick(1000.2)); assert len(sup["nav"]) == n1             # backing off
    run(s.tick(1000.0 + 120)); assert len(sup["nav"]) == n1 + 1
    sup["state"] = "playing"
    run(s.tick(1300.0)); assert s.reconnector.attempt == 0        # recovered -> counters reset


def test_supervisor_restarts_crashed_browser_eventually(sup):
    s = edumix_api.supervisor
    sup["state"] = "browser_down"
    t = 1000.0
    for _ in range(6):
        t += 200
        run(s.tick(t))
    assert ("browser-source", "restart") in sup["units"]


def test_supervisor_leaves_human_states_alone(sup):
    s = edumix_api.supervisor
    for st in ("login_required", "awaiting_otp", "session_expired", "protected"):
        sup["state"] = st
        run(s.tick(5000.0))
    assert sup["nav"] == [] and sup["units"] == []


def test_supervisor_paused_after_stop_does_nothing(sup):
    s = edumix_api.supervisor
    s.paused = True
    sup["state"] = "browser_down"
    for i in range(6):
        run(s.tick(1000.0 + i * 500))
    assert sup["units"] == [] and sup["nav"] == []


def test_supervisor_idle_when_edumix_not_active(client, monkeypatch):
    called = []
    async def st(rc=None): called.append(1); return {}
    monkeypatch.setattr(edumix, "status", st)
    assert run(edumix_api.supervisor.tick(1.0)) is None and called == []


def test_status_snapshot_maps_cdp_failure_to_browser_down(monkeypatch):
    async def boom(expr, user_gesture=False): raise cdp.CDPError("no port")
    monkeypatch.setattr(cdp, "evaluate", boom)
    st = run(edumix.status())
    assert st["state"] == "browser_down" and st["level"] == "bad"


def test_supervisor_does_not_kill_a_chrome_that_is_still_starting(sup):
    """Open a class, Chrome takes ~30 s to come up: browser_down inside the grace window is left alone."""
    s = edumix_api.supervisor
    s.mark_started(now=1000.0)
    sup["state"] = "browser_down"
    for i in range(1, 8):                      # 7 checks across the first 35 s
        run(s.tick(1000.0 + i * 5))
    assert sup["units"] == []
    for i in range(1, 12):                     # after the grace period it may recover the browser
        run(s.tick(1060.0 + i * 120))
    assert ("browser-source", "restart") in sup["units"]


def test_restart_starts_a_new_grace_window(sup):
    s = edumix_api.supervisor
    sup["state"] = "browser_down"
    t = 5000.0
    while ("browser-source", "restart") not in sup["units"]:
        t += 200
        run(s.tick(t))
    assert s.grace_until == t + config.EDUMIX_START_GRACE_SECONDS
    for dt_ in (5, 20, 45):                    # still inside the window: no second restart
        run(s.tick(t + dt_))
    assert sup["units"].count(("browser-source", "restart")) == 1


def test_unknown_state_is_never_acted_on_or_counted_as_signed_in(sup):
    s = edumix_api.supervisor
    sup["state"] = "unknown"
    run(s.tick(100.0))
    assert sup["nav"] == [] and sup["units"] == []
    assert not db.get_setting(edumix_api.SETTING_LAST_AUTH)


def test_status_probe_is_shared_and_bounded(monkeypatch):
    """The page poll, the supervisor and every open tab share one DevTools probe per STATUS_TTL."""
    import json
    calls = []

    async def ev(expr, user_gesture=False):
        calls.append(1)
        return {"value": json.dumps(snap())}
    monkeypatch.setattr(cdp, "evaluate", ev)
    for _ in range(25):                       # 25 callers inside one window
        assert run(edumix.status())["state"] == "authenticated"
    assert len(calls) == 1
    edumix.invalidate()
    run(edumix.status())
    assert len(calls) == 2


def test_actions_invalidate_the_probe_cache(monkeypatch):
    import json
    calls = []

    async def ev(expr, user_gesture=False):
        calls.append(expr[:12])
        return {"value": json.dumps(snap()) if "objectFit" not in expr else 1}

    async def nav(url): pass
    monkeypatch.setattr(cdp, "evaluate", ev); monkeypatch.setattr(cdp, "navigate", nav)
    run(edumix.status()); n = len(calls)
    run(edumix.navigate_class(CLASS)); run(edumix.status())
    assert len(calls) == n + 1                # navigating forced a fresh probe
    run(edumix.navigate_login()); run(edumix.status()); assert len(calls) == n + 2
    run(edumix.set_video_fit("fill")); run(edumix.status())
    assert len(calls) == n + 4                # the fit call itself + a fresh probe


def test_probe_failure_is_cached_briefly_too(monkeypatch):
    calls = []

    async def boom(expr, user_gesture=False):
        calls.append(1); raise cdp.CDPError("down")
    monkeypatch.setattr(cdp, "evaluate", boom)
    assert run(edumix.status())["state"] == "browser_down"
    assert run(edumix.status())["state"] == "browser_down"
    assert len(calls) == 1


def test_supervisor_clears_start_limit_before_every_restart(sup):
    """browser-source has StartLimitBurst=5/300s: recovery must reset-failed first or it can lock itself out."""
    s = edumix_api.supervisor
    sup["state"] = "browser_down"
    t = 9000.0
    while sup["units"].count(("browser-source", "restart")) < 2:
        t += 300
        run(s.tick(t))
    units = sup["units"]
    for i, u in enumerate(units):
        if u == ("browser-source", "restart"):
            assert units[i - 1] == ("browser-source", "reset-failed")


def test_status_after_operator_stop_reads_stopped_not_an_error(client, monkeypatch):
    monkeypatch.setattr(control, "start_source", lambda s: {"results": [], "rtmp_dropped": False, "hot_swapped": False})
    client.post("/api/edumix/open", json={"url": CLASS}, headers=client.csrf)

    async def boom(expr, user_gesture=False): raise cdp.CDPError("no browser")
    monkeypatch.setattr(cdp, "evaluate", boom)
    assert client.get("/api/edumix/status").json()["state"] == "browser_down"        # a crash is still an error
    monkeypatch.setattr(control, "unit_action", lambda u, v: {"ok": True})
    client.post("/api/edumix/stop", json={}, headers=client.csrf)
    edumix.invalidate()
    j = client.get("/api/edumix/status").json()
    assert j["state"] == "stopped" and j["level"] == "idle" and j["active"] is True


def test_expiry_needs_consecutive_confirmation():
    edumix._expiry.update(state=None, n=0)
    assert edumix.confirm_expiry("access_expired") == "loading"        # first sighting: not reported
    assert edumix.confirm_expiry("access_expired") == "access_expired"
    assert edumix.confirm_expiry("playing") == "playing"               # any other state resets the count
    assert edumix.confirm_expiry("session_expired") == "loading"
    assert edumix.confirm_expiry("access_expired") == "loading"        # a different expiry kind restarts it
    edumix._expiry.update(state=None, n=0)


def test_access_expired_is_not_a_login_problem():
    assert edumix.describe("access_expired")["needs_human"]
    assert "access_expired" in edumix_api._AUTHENTICATED_FAMILY       # the login is still valid
    assert "session_expired" not in edumix_api._AUTHENTICATED_FAMILY


# ------------------------------------------------------------------ source worker lifecycle (real Supervisor, mocked page)

def test_worker_recovers_a_dead_browser_with_backoff_then_restarts_it(sup):
    from app import edumix_worker, jobs
    w = edumix_worker.Worker(edumix_api.supervisor)
    sup["state"] = "browser_down"
    t = 1000.0
    run(w.tick(t))                                                 # new job adopted
    edumix_api.supervisor.grace_until = 0.0
    for i in range(1, 40):
        run(w.tick(t + i * 70))
        if ("browser-source", "restart") in sup["units"]:
            break
    assert ("browser-source", "restart") in sup["units"]           # escalated from reload attempts to a restart
    assert jobs.current()["state"] == "browser_down"               # health is visible to the dashboard meanwhile
    sup["state"] = "playing"
    run(w.tick(t + 5000))
    assert jobs.current()["state"] == "playing" and jobs.worker_alive(jobs.current())


def test_worker_gives_up_after_bounded_attempts_and_does_not_loop_forever(sup):
    from app import edumix_worker
    w = edumix_worker.Worker(edumix_api.supervisor)
    sup["state"] = "network_down"
    run(w.tick(1000.0))
    for i in range(1, 400):
        run(w.tick(1000.0 + i * 70))
    restarts = sup["units"].count(("browser-source", "restart"))
    assert restarts <= 4                                           # bounded: it stops trying, it does not hammer the box
    assert edumix_api.supervisor.reconnector.snapshot(1000.0 + 400 * 70)["gave_up"]


def test_worker_never_acts_on_states_a_person_must_resolve(sup):
    from app import edumix_worker
    w = edumix_worker.Worker(edumix_api.supervisor)
    for st in ("login_required", "awaiting_otp", "session_expired", "access_expired", "protected"):
        sup["state"] = st; sup["path"] = "/login"
        before = (list(sup["units"]), list(sup["nav"]))
        for i in range(8):
            run(w.tick(2000.0 + i * 100))
        after = (list(sup["units"]), list(sup["nav"]))
        assert after == before, st                                  # no restart, no navigation, no OTP/login automation


def test_worker_restart_keeps_the_stop(sup):
    from app import edumix_worker, jobs
    w1 = edumix_worker.Worker(edumix_api.supervisor)
    sup["state"] = "playing"
    run(w1.tick(1000.0))
    jobs.request_stop("admin")
    w2 = edumix_worker.Worker(edumix_api.supervisor)                # worker process restarted (crash/redeploy)
    sup["state"] = "browser_down"
    for i in range(10):
        run(w2.tick(3000.0 + i * 100))
    assert ("browser-source", "restart") not in sup["units"]         # the explicit Stop survives a worker restart


def test_job_endpoint_reattaches_after_dashboard_restart(client, sup):
    from app import edumix_worker
    w = edumix_worker.Worker(edumix_api.supervisor)
    sup["state"] = "playing"
    run(w.tick(1000.0))
    d = client.get("/api/edumix/job").json()                       # a "reopened dashboard" only reads durable state
    assert d["job"]["desired"] == "running" and d["job"]["class_path"].startswith("/class/")
    assert d["job"]["broadcast"] is False and d["capture"]["enabled"] is False
    assert "token" not in str(d).lower() and "cookie" not in str(d).lower()
    client.post("/api/edumix/stop", headers=client.csrf)
    assert client.get("/api/edumix/job").json()["job"]["desired"] == "stopped"
