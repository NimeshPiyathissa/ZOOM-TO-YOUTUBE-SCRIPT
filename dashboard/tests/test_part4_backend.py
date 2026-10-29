"""Backend Part 4 (theme persistence, restart-all, reboot readiness) on top of
the production baseline: migration safety, input handling, auth/CSRF on every
new route, the restart-all lock, reboot gating, and that the code's
`systemctl show` property list matches what the sudoers installer grants."""
import re
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app.main import app
from app import config, control, db, deps, security


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_dashboard.db")
    deps._rate_buckets.clear()
    yield
    deps._rate_buckets.clear()


def _make_user(username="admin"):
    db.init_db()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, security.hash_password("correct-horse-battery-staple"), time.time()),
        )
        return cur.lastrowid


@pytest.fixture
def client():
    user_id = _make_user()
    session_id, csrf_token = security.create_session(user_id, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: session_id})
    c.csrf = csrf_token
    c.user_id = user_id
    return c


@pytest.fixture
def anon():
    db.init_db()
    return TestClient(app)


# ---------------------------------------------------------------- migration

def test_migration_adds_theme_to_existing_db_and_keeps_rows(tmp_path):
    # A users table exactly as production has it today (no theme column).
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('set-password', 'h', 1.0)")
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # idempotent: a second start must not fail or duplicate anything

    conn = sqlite3.connect(config.DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    rows = conn.execute("SELECT username, password_hash, theme FROM users").fetchall()
    conn.close()
    assert cols.count("theme") == 1
    assert rows == [("set-password", "h", None)]


def test_pre_patch_code_shape_still_reads_migrated_db():
    """Rollback safety: old code never names the new column, so a migrated DB
    must still satisfy its queries."""
    _make_user()
    with db.get_conn() as conn:
        row = conn.execute("SELECT id, username, password_hash, created_at FROM users WHERE username='admin'").fetchone()
    assert row["username"] == "admin"


# ---------------------------------------------------------------- /api/theme

def test_theme_anonymous_sets_cookie_only(anon):
    res = anon.post("/api/theme", json={"theme": "white"})
    assert res.status_code == 200
    assert res.cookies.get("zsdash_theme") == "white"
    assert "httponly" in res.headers["set-cookie"].lower()


@pytest.mark.parametrize("payload", [
    {"theme": "purple"}, {"theme": ""}, {}, [], "white", 5, None,
])
def test_theme_rejects_bad_values_with_400(anon, payload):
    assert anon.post("/api/theme", json=payload).status_code == 400


def test_theme_malformed_json_is_400_not_500(anon):
    res = anon.post("/api/theme", content=b"{not json", headers={"content-type": "application/json"})
    assert res.status_code == 400


def test_theme_logged_in_requires_csrf(client):
    assert client.post("/api/theme", json={"theme": "black"}).status_code == 403
    with db.get_conn() as conn:
        assert conn.execute("SELECT theme FROM users WHERE id=?", (client.user_id,)).fetchone()["theme"] is None


def _theme_in_context(client_or_anon, path="/login"):
    """What the context processor hands every template (base.html turns it
    into <html data-theme>). Checked directly so the test doesn't depend on
    which template set is on disk."""
    from app import main
    from starlette.requests import Request
    cookies = "; ".join(f"{k}={v}" for k, v in client_or_anon.cookies.items())
    scope = {"type": "http", "method": "GET", "path": path, "headers": [(b"cookie", cookies.encode())], "query_string": b""}
    req = Request(scope)
    ctx = {}
    for proc in main.templates.context_processors:
        ctx.update(proc(req))
    return ctx.get("theme")


def test_theme_logged_in_persists_to_db_and_renders(client):
    res = client.post("/api/theme", json={"theme": "black"}, headers={"X-CSRF-Token": client.csrf})
    assert res.status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT theme FROM users WHERE id=?", (client.user_id,)).fetchone()["theme"] == "black"
    assert client.get("/").status_code == 200
    assert _theme_in_context(client, "/") == "black"
    # A browser without the cookie (another device) still gets the DB value.
    client.cookies.delete("zsdash_theme")
    assert _theme_in_context(client, "/") == "black"


def test_login_page_renders_without_theme_and_with_cookie(anon):
    assert anon.get("/login").status_code == 200
    assert _theme_in_context(anon) is None
    anon.cookies.set("zsdash_theme", "white")
    assert anon.get("/login").status_code == 200
    assert _theme_in_context(anon) == "white"
    anon.cookies.set("zsdash_theme", "<script>")  # not whitelisted -> ignored
    assert _theme_in_context(anon) is None


def test_saved_theme_applied_at_login_for_new_browser():
    user_id = _make_user("bob")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET theme='white' WHERE id=?", (user_id,))
    c = TestClient(app)
    res = c.post("/login", data={"username": "bob", "password": "correct-horse-battery-staple"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.cookies.get("zsdash_theme") == "white"


# ---------------------------------------------------------------- auth on new routes

@pytest.mark.parametrize("method,path", [
    ("get", "/api/system/readiness"),
    ("get", "/api/system/restart-progress"),
    ("post", "/api/system/restart-all"),
])
def test_new_system_routes_require_session(anon, method, path):
    assert getattr(anon, method)(path).status_code == 401


def test_restart_all_requires_csrf(client):
    with patch.object(control, "full_restart") as fr:
        assert client.post("/api/system/restart-all", json={}).status_code == 403
    fr.assert_not_called()


def test_readiness_returns_checks(client):
    fake = [{"check": "a", "ok": True, "detail": ""}, {"check": "b", "ok": False, "detail": "x"}]
    with patch.object(control, "reboot_readiness", return_value=fake):
        res = client.get("/api/system/readiness")
    assert res.status_code == 200
    assert res.json() == {"checks": fake, "ok": False}


# ---------------------------------------------------------------- restart-all behaviour

def _stream(phase):
    return lambda unit: {"phase": phase}


def test_restart_all_refuses_when_live_without_confirm_and_releases_lock(client):
    h = {"X-CSRF-Token": client.csrf}
    with patch.object(control, "unit_show", side_effect=_stream(control.PHASE_LIVE)), \
         patch.object(control, "full_restart") as fr:
        r1 = client.post("/api/system/restart-all", json={}, headers=h)
        r2 = client.post("/api/system/restart-all", json={}, headers=h)
    assert r1.status_code == 409 and "live" in r1.json()["detail"].lower()
    # The lock was released after the refusal: the 2nd call is refused for
    # the same reason, not "already in progress".
    assert r2.status_code == 409 and "live" in r2.json()["detail"].lower()
    fr.assert_not_called()


def test_restart_all_is_single_flight(client):
    h = {"X-CSRF-Token": client.csrf}
    gate, entered = threading.Event(), threading.Event()

    def slow_restart():
        entered.set()
        gate.wait(5)

    with patch.object(control, "unit_show", side_effect=_stream(control.PHASE_STOPPED)), \
         patch.object(control, "full_restart", side_effect=slow_restart):
        r1 = client.post("/api/system/restart-all", json={}, headers=h)
        assert r1.status_code == 200
        assert entered.wait(5)
        deps._rate_buckets.clear()
        r2 = client.post("/api/system/restart-all", json={}, headers=h)
        assert r2.status_code == 409 and "in progress" in r2.json()["detail"]
        gate.set()
        for _ in range(50):
            if not __import__("app.main", fromlist=["x"])._restart_all_lock.locked():
                break
            time.sleep(0.05)
        deps._rate_buckets.clear()
        r3 = client.post("/api/system/restart-all", json={"confirm_live": True}, headers=h)
    assert r3.status_code == 200
    gate.set()


def test_reboot_refused_when_readiness_fails(client):
    h = {"X-CSRF-Token": client.csrf}
    with patch.object(control, "reboot_readiness", return_value=[{"check": "x", "ok": False, "detail": ""}]), \
         patch.object(control, "reboot_vps") as rv:
        res = client.post("/api/reboot", json={"confirm": "REBOOT"}, headers=h)
    assert res.status_code == 409
    rv.assert_not_called()


def test_reboot_needs_confirmation_token(client):
    h = {"X-CSRF-Token": client.csrf}
    with patch.object(control, "reboot_vps") as rv:
        assert client.post("/api/reboot", json={}, headers=h).status_code == 400
    rv.assert_not_called()


# ---------------------------------------------------------------- control helpers

def test_units_for_webpage_source_skip_zoom():
    with patch.object(control, "read_current_source", return_value={"SOURCE_TYPE": "webpage"}):
        units = control._units_for_active_source()
    assert "zoom" not in units and "browser-source" in units
    assert units == [u for u in config.UNIT_ORDER if u != "zoom"]


def test_units_for_zoom_web_join_use_browser_source():
    with patch.object(control, "read_current_source", return_value={"SOURCE_TYPE": "zoom", "ZOOM_JOIN_VIA": "web"}):
        units = control._units_for_active_source()
    assert "zoom" not in units and "browser-source" in units


# ---------------------------------------------------------------- sudoers <-> code

def test_show_properties_match_installer_sudoers_grant():
    """sudo matches argv literally: if control.SHOW_PROPERTIES and the
    installer's SHOW_PROPS ever disagree, every unit status read is silently
    denied and the dashboard shows the whole pipeline as stopped."""
    installer = (Path(__file__).resolve().parents[1] / "install-dashboard.sh").read_text(encoding="utf-8")
    m = re.search(r'^SHOW_PROPS="([^"]+)"', installer, re.M)
    assert m, "SHOW_PROPS not found in install-dashboard.sh"
    assert m.group(1).replace("\\,", ",") == control.SHOW_PROPERTIES


def test_root_grants_match_commands_the_code_runs():
    installer = (Path(__file__).resolve().parents[1] / "install-dashboard.sh").read_text(encoding="utf-8")
    assert f"NOPASSWD: {control.SYSTEMCTL} restart {control.DASHBOARD_UNIT}" in installer
    assert f"NOPASSWD: /usr/bin/loginctl show-user {config.ZOOMBOT_USER} --property=Linger" in installer
