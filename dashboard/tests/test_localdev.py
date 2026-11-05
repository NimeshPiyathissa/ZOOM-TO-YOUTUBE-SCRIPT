"""Windows local-development adapter (app/localdev.py + dev/windows/*): isolation, safety and wiring.

No real browser is started and nothing is executed: psutil / subprocess are faked. What these tests cannot show
(real Chrome window, real MJPhysics login, DPAPI-encrypted profile) is exercised by hand - see
docs/local-windows-dev.md."""
import importlib.util
import logging
import pathlib
import shutil
import subprocess
import time
import types

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app import config, control, db, deps, edumix, localdev, security

REPO = pathlib.Path(__file__).resolve().parents[2]
CLASS = "https://mjphysics.lk/class/0123456789abcdef01234567"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pre.db")
    b = localdev.install(tmp_path / "home", chrome_port=9333, chrome_path=r"C:\fake\chrome.exe")
    yield b
    localdev.uninstall()


class FakeProc:
    def __init__(self, cmdline, create_time=None):
        self._cmd = cmdline
        self.info = {"pid": id(self) % 99999, "cmdline": cmdline}
        self.terminated = False
        self.killed = False
        self._ct = create_time or time.time() - 100

    def cmdline(self):
        return self._cmd

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def create_time(self):
        return self._ct


# ------------------------------------------------------------------ redaction

def test_redact_text_masks_every_credential_shape():
    raw = ("GET https://mjphysics.lk/class/abc?token=SECRETTOKEN&x=1 Cookie: sid=abc123def456 "
           "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345 phone +94700000000 otp=482913 password: hunter2 "
           "csrf 0123456789abcdef0123456789abcdef0123456789")
    out = localdev.redact_text(raw)
    for leaked in ("SECRETTOKEN", "abc123def456", "abcdefghijklmnopqrstuvwxyz012345", "94700000000", "482913", "hunter2",
                   "0123456789abcdef0123456789abcdef0123456789"):
        assert leaked not in out
    assert "mjphysics.lk/class/abc" in out            # the path stays useful for diagnostics


def test_redacting_filter_cleans_log_records(caplog):
    f = localdev.RedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "login otp=%s cookie=%s", ("482913", "sid=xyz"), None)
    assert f.filter(rec) is True
    assert "482913" not in rec.getMessage() and "sid=xyz" not in rec.getMessage() and rec.args == ()


# ------------------------------------------------------------------ the local browser

def test_command_is_isolated_loopback_and_unautomated(tmp_path):
    b = localdev.LocalBrowser(r"C:\x\chrome.exe", tmp_path / "prof", 9333)
    cmd = b.command(CLASS)
    assert cmd[0] == r"C:\x\chrome.exe" and cmd[-1] == CLASS
    assert f"--user-data-dir={tmp_path / 'prof'}" in cmd
    assert "--remote-debugging-port=9333" in cmd and "--remote-debugging-address=127.0.0.1" in cmd
    for forbidden in ("--kiosk", "--headless", "--disable-web-security", "--no-sandbox", "--enable-automation",
                      "--ignore-certificate-errors", "--remote-allow-origins=*"):
        assert not any(a.startswith(forbidden) for a in cmd), forbidden


def test_extra_browser_flags_are_allowlisted_by_denylist(tmp_path, monkeypatch):
    b = localdev.LocalBrowser("c.exe", tmp_path / "p", 9333)
    monkeypatch.setenv("ZS_CHROME_FLAGS", "--headless=new --window-size=800,600")
    cmd = b.command(CLASS)
    assert "--headless=new" in cmd and cmd.index("--headless=new") < cmd.index(CLASS) and cmd[-1] == CLASS
    for bad in ("--no-sandbox", "--disable-web-security", "--remote-debugging-address=0.0.0.0", "--remote-allow-origins=*",
                "--user-data-dir=C:\\other", "--load-extension=x", "--proxy-server=evil:1", "notaflag"):
        monkeypatch.setenv("ZS_CHROME_FLAGS", bad)
        with pytest.raises(ValueError, match="not allowed"):
            b.command(CLASS)
    monkeypatch.delenv("ZS_CHROME_FLAGS")
    assert "--headless=new" not in b.command(CLASS)


def test_start_only_opens_mjphysics_or_blank(tmp_path, monkeypatch):
    b = localdev.LocalBrowser("c.exe", tmp_path / "p", 9333)
    popen = []
    monkeypatch.setattr(localdev.subprocess, "Popen", lambda *a, **k: popen.append(a) or None)
    monkeypatch.setattr(b, "stop", lambda: 0)
    for bad in ("https://example.com/", "http://mjphysics.lk/", "file:///c:/windows/win.ini", "javascript:alert(1)",
                "https://mjphysics.lk@evil.com/"):
        with pytest.raises(edumix.EduMixError):
            b.start(bad)
    assert popen == []
    b.start("about:blank"); b.start(CLASS)
    assert [a[0][-1] for a in popen] == ["about:blank", CLASS]


def test_start_closes_its_own_profile_before_launching(tmp_path, monkeypatch):
    order = []
    b = localdev.LocalBrowser("c.exe", tmp_path / "p", 9333)
    monkeypatch.setattr(b, "stop", lambda: order.append("stop") or 1)
    monkeypatch.setattr(localdev.subprocess, "Popen", lambda *a, **k: order.append("launch"))
    b.start(CLASS)
    assert order == ["stop", "launch"]        # a second launch on an open profile would ignore the DevTools flags


def test_only_processes_with_our_profile_are_ever_touched(tmp_path, monkeypatch):
    prof = tmp_path / "chrome-profiles" / "edumix"
    mine_main = FakeProc([r"C:\c\chrome.exe", f"--user-data-dir={prof}", "--remote-debugging-port=9333"])
    mine_gpu = FakeProc([r"C:\c\chrome.exe", "--type=gpu-process", f"--user-data-dir={prof}"])
    users_chrome = FakeProc([r"C:\c\chrome.exe", "--profile-directory=Default"])               # your everyday Chrome
    users_renderer = FakeProc([r"C:\c\chrome.exe", "--type=renderer", r"--user-data-dir=C:\Users\x\AppData\Local\Google\Chrome\User Data"])
    other_profile = FakeProc([r"C:\c\chrome.exe", f"--user-data-dir={tmp_path / 'another'}"])
    unrelated = FakeProc(["notepad.exe"])
    allp = [mine_main, mine_gpu, users_chrome, users_renderer, other_profile, unrelated]
    monkeypatch.setattr(localdev.psutil, "process_iter", lambda attrs=None: allp)
    monkeypatch.setattr(localdev.psutil, "wait_procs", lambda procs, timeout=None: ([], []))
    b = localdev.LocalBrowser("c.exe", prof, 9333)
    assert {id(p) for p in b.processes()} == {id(mine_main), id(mine_gpu)}
    assert b.main_process() is mine_main                       # the process without --type= is the browser itself
    assert b.stop() == 2
    assert mine_main.terminated and mine_gpu.terminated
    for safe in (users_chrome, users_renderer, other_profile, unrelated):
        assert not safe.terminated and not safe.killed


def test_profile_match_survives_path_case_and_slash_differences(tmp_path, monkeypatch):
    prof = tmp_path / "Chrome-Profiles" / "EduMix"
    p = FakeProc(["chrome.exe", f"--user-data-dir={str(prof).lower().replace(chr(92), '/')}"])
    monkeypatch.setattr(localdev.psutil, "process_iter", lambda attrs=None: [p])
    assert localdev.LocalBrowser("c.exe", prof, 9333).is_running() is (localdev.os.name == "nt")


def test_phase_reports_stopped_starting_live_failed(tmp_path, monkeypatch):
    b = localdev.LocalBrowser("c.exe", tmp_path / "p", 9333)
    monkeypatch.setattr(b, "main_process", lambda: None)
    assert b.phase() == "STOPPED"
    young = FakeProc(["c"], create_time=time.time() - 3)
    old = FakeProc(["c"], create_time=time.time() - 300)
    monkeypatch.setattr(b, "main_process", lambda: young)
    monkeypatch.setattr(localdev, "port_open", lambda p, timeout=0.25: False)
    assert b.phase() == "STARTING"
    monkeypatch.setattr(b, "main_process", lambda: old)
    assert b.phase() == "FAILED"
    monkeypatch.setattr(localdev, "port_open", lambda p, timeout=0.25: True)
    assert b.phase() == "LIVE"


def test_find_chrome_prefers_override_then_chrome_then_edge(tmp_path, monkeypatch):
    fake = tmp_path / "mychrome.exe"; fake.write_text("x")
    monkeypatch.setenv("ZS_CHROME_PATH", str(fake))
    assert localdev.find_chrome() == str(fake)
    monkeypatch.delenv("ZS_CHROME_PATH")
    pf = tmp_path / "pf"
    (pf / "Google" / "Chrome" / "Application").mkdir(parents=True); (pf / "Google" / "Chrome" / "Application" / "chrome.exe").write_text("x")
    (pf / "Microsoft" / "Edge" / "Application").mkdir(parents=True); (pf / "Microsoft" / "Edge" / "Application" / "msedge.exe").write_text("x")
    for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.setenv(k, str(pf))
    assert localdev.find_chrome().endswith("chrome.exe")
    (pf / "Google" / "Chrome" / "Application" / "chrome.exe").unlink()
    assert localdev.find_chrome().endswith("msedge.exe")


def test_free_port_skips_busy_ports():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); busy = s.getsockname()[1]
    try:
        assert localdev.free_port(busy, busy + 20) != busy
    finally:
        s.close()


# ------------------------------------------------------------------ install: isolation + safety

def test_install_redirects_every_path_into_the_dev_home_not_the_repo(installed, tmp_path):
    home = tmp_path / "home"
    for name in ("DATA_DIR", "DB_PATH", "OVERLAY_CONFIG_FILE", "BRB_SLATE_FILE", "SECRET_STORE_FILE", "VAULT_LOCK_SENTINEL_FILE",
                 "SETTINGS_FILE", "RECORDINGS_DIR"):
        p = pathlib.Path(getattr(config, name))
        assert home in p.parents, name
        assert REPO not in p.parents, name
    assert config.CHROME_DEBUG_PORT == 9333 and config.LOCAL_DEV is True
    assert installed.profile_dir == home / "chrome-profiles" / "edumix"


def test_sudo_is_never_executed(installed, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a real process was started"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a real process was started"))
    r = control.run_as_zoombot([control.SUDO, "-u", "zoombot", "/bin/rm", "-rf", "/"])
    assert r.returncode == 0 and r.stdout == b""
    from app import env_store
    assert env_store.run_as_zoombot is control.run_as_zoombot            # modules that imported it by name are patched too


def test_encoder_can_never_start_locally(installed):
    for verb in ("start", "restart"):
        with pytest.raises(control.ControlError, match="Local development"):
            control.unit_action("ffmpeg-stream", verb)
    assert control.unit_action("ffmpeg-stream", "stop")["ok"]
    assert control.unit_show("ffmpeg-stream")["phase"] == "STOPPED"
    assert control._ffmpeg_is_up() is False
    assert config.EDUMIX_CAPTURE_ENABLED is False and edumix.capture_blocked("edumix")


def test_install_refuses_if_capture_were_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EDUMIX_CAPTURE_ENABLED", True)
    with pytest.raises(RuntimeError, match="must be False"):
        localdev.install(tmp_path / "h", chrome_path=r"C:\fake\chrome.exe")
    localdev.uninstall()


def test_source_roundtrip_and_browser_unit_uses_the_edumix_url(installed, monkeypatch):
    started = []
    monkeypatch.setattr(installed, "start", lambda url: started.append(url))
    monkeypatch.setattr(installed, "stop", lambda: started.append("STOP") or 1)
    src = {"type": "edumix", "url": CLASS, "options": {}, "account_id": None, "name": "EduMix Live"}
    control.write_current_source(src)
    cur = control.read_current_source()
    assert cur["SOURCE_TYPE"] == "edumix" and cur["EDUMIX_URL"] == CLASS
    control.unit_action("browser-source", "restart")
    control.unit_action("browser-source", "stop")
    assert started == [CLASS, "STOP"]
    # a non-EduMix source never makes the local adapter open an arbitrary page
    control.write_current_source({"type": "webpage", "url": "https://example.com/", "options": {}, "account_id": None, "name": "w"})
    control.unit_action("browser-source", "start")
    assert started[-1] == "about:blank"


def test_unit_show_shape_matches_the_real_one_and_other_units_are_simulated(installed, monkeypatch):
    real_keys = {"unit", "active_state", "sub_state", "result", "restart_count", "exec_main_status", "exec_main_code",
                 "uptime_seconds", "main_pid", "unit_file_state", "phase"}
    for unit in ("zoom", "xvfb", "browser-source", "ffmpeg-stream"):
        assert set(control.unit_show(unit)) == real_keys
    assert control.unit_show("zoom")["unit_file_state"] == "simulated" and control.unit_show("zoom")["phase"] == "STOPPED"
    assert control.unit_action("xvfb", "start")["ok"]
    with pytest.raises(control.ControlError):
        control.unit_action("not-a-unit", "start")


def test_start_source_for_edumix_launches_only_the_browser(installed, monkeypatch):
    started = []
    monkeypatch.setattr(installed, "start", lambda url: started.append(url))
    monkeypatch.setattr(installed, "stop", lambda: 0)
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "DB_PATH", pathlib.Path(config.DATA_DIR) / "x.db")
    db.init_db()
    from app import sources
    sid = sources.create_source("EduMix Live", "edumix", CLASS, {})
    out = control.start_source(sources.get_source(sid))
    assert started == [CLASS] and out["rtmp_dropped"] is False
    assert all(r.get("unit") != "ffmpeg-stream" for r in out["results"])


def test_diagnosis_messages(installed, monkeypatch):
    monkeypatch.setattr(installed, "is_running", lambda: False)
    assert control.browser_diagnosis()["code"] == "unit_down"
    monkeypatch.setattr(installed, "is_running", lambda: True)
    monkeypatch.setattr(localdev, "port_open", lambda p, timeout=0.25: False)
    assert control.browser_diagnosis()["code"] == "starting"
    monkeypatch.setattr(localdev, "port_open", lambda p, timeout=0.25: True)
    assert control.browser_diagnosis()["code"] == "port_closed"


def test_uninstall_restores_everything(tmp_path):
    before = (control.unit_action, control.run_as_zoombot, config.DATA_DIR, config.CHROME_DEBUG_PORT)
    localdev.install(tmp_path / "h", chrome_path=r"C:\fake\chrome.exe")
    assert control.unit_action is not before[0]
    localdev.uninstall()
    assert (control.unit_action, control.run_as_zoombot, config.DATA_DIR, config.CHROME_DEBUG_PORT) == before
    assert config.LOCAL_DEV is False and localdev.browser is None


def test_double_install_is_refused(installed, tmp_path):
    with pytest.raises(RuntimeError, match="already installed"):
        localdev.install(tmp_path / "again", chrome_path=r"C:\fake\chrome.exe")


# ------------------------------------------------------------------ routes + production defaults

@pytest.fixture
def mini(tmp_path, monkeypatch):
    """The local routes mounted on a throw-away app, with real session/CSRF handling."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "m.db")
    db.init_db()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    deps._rate_buckets.clear()
    app = FastAPI(); app.include_router(localdev.router)
    c = TestClient(app, cookies={config.COOKIE_NAME: sid}); c.csrf = {"x-csrf-token": csrf}
    c.anon = TestClient(app)
    localdev._frame.update(at=0.0, jpg=None)
    return c


def test_preview_requires_login_and_serves_a_jpeg(mini, monkeypatch):
    assert mini.anon.get("/api/edumix/local/preview.jpg").status_code == 401

    async def cap(quality=55):
        return b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    monkeypatch.setattr(localdev, "capture_jpeg", cap)
    r = mini.get("/api/edumix/local/preview.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"\xff\xd8")


def test_preview_failure_is_a_503_with_no_secrets(mini, monkeypatch):
    async def cap(quality=55):
        raise RuntimeError("boom cookie=sid123 otp=482913")
    monkeypatch.setattr(localdev, "capture_jpeg", cap)
    r = mini.get("/api/edumix/local/preview.jpg")
    assert r.status_code == 503 and "482913" not in r.text and "sid123" not in r.text


def test_preview_frames_are_cached_briefly(monkeypatch):
    import asyncio, base64, json
    calls = []

    class WS:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    async def target(): return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}

    async def call(ws, i, method, params=None):
        calls.append(method)
        return {"data": base64.b64encode(b"\xff\xd8jpg").decode()}
    import websockets
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: WS())
    monkeypatch.setattr(localdev.cdp, "_get_page_target", target)
    monkeypatch.setattr(localdev.cdp, "_call", call)
    localdev._frame.update(at=0.0, jpg=None)
    loop = asyncio.new_event_loop()
    try:
        a = loop.run_until_complete(localdev.capture_jpeg()); b = loop.run_until_complete(localdev.capture_jpeg())
    finally:
        loop.close()
    assert a == b == b"\xff\xd8jpg" and calls == ["Page.captureScreenshot"]


def test_focus_needs_csrf(mini, monkeypatch):
    assert mini.post("/api/edumix/local/focus").status_code == 403

    async def front(): pass
    monkeypatch.setattr(localdev, "bring_to_front", front)
    assert mini.post("/api/edumix/local/focus", headers=mini.csrf).json() == {"ok": True}


def test_production_app_has_no_local_routes_or_flags():
    from app.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [p for p in paths if p.startswith("/api/edumix/local")]
    assert config.LOCAL_DEV is False and config.EDUMIX_CAPTURE_ENABLED is False


@pytest.fixture
def page_client(tmp_path, monkeypatch):
    from app.main import app
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "p.db")
    db.init_db()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, _ = security.create_session(uid, "127.0.0.1")
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    return TestClient(app, cookies={config.COOKIE_NAME: sid})


def test_page_is_vnc_mode_by_default_and_local_mode_when_enabled(page_client, monkeypatch):
    html = page_client.get("/edumix").text
    assert 'data-view="vnc"' in html and "em-local-badge" not in html and "local-dev-banner" not in html
    monkeypatch.setattr(config, "LOCAL_DEV", True)
    html = page_client.get("/edumix").text
    assert 'data-view="local"' in html and "em-local-badge" in html and "Local Windows mode" in html
    assert "capture off" in html and "disabled" in html            # the locked YouTube card is still there


def test_banner_appears_on_every_page_when_the_dev_server_sets_the_global(page_client):
    from app.main import templates
    assert "local-dev-banner" not in page_client.get("/audit").text            # production default: no banner
    templates.env.globals["local_dev"] = True
    try:
        assert "local-dev-banner" in page_client.get("/audit").text            # a page that relies on the global alone
        assert "Nothing on this page touches production" in page_client.get("/audit").text
    finally:
        templates.env.globals.pop("local_dev", None)


# ------------------------------------------------------------------ launcher files

def _load_dev_server():
    spec = importlib.util.spec_from_file_location("zs_dev_server", REPO / "dev" / "windows" / "dev_server.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_dev_home_honours_override(monkeypatch, tmp_path):
    ds = _load_dev_server()
    monkeypatch.setenv("ZS_DEV_HOME", str(tmp_path / "x"))
    assert ds.dev_home() == tmp_path / "x"
    monkeypatch.delenv("ZS_DEV_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert ds.dev_home() == tmp_path / "lad" / "ZoomToYT-Dev"


def test_dev_server_never_targets_the_repo_or_production_paths():
    src = (REPO / "dev" / "windows" / "dev_server.py").read_text(encoding="utf-8")
    for forbidden in ("13.233.105.162", "54.254.180.52", "missakaart", "ssh ", "scp ", "/home/zoombot", "EDUMIX_CAPTURE_ENABLED = True"):
        assert forbidden not in src


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell not available")
def test_launcher_script_parses_cleanly():
    ps = str(REPO / "dev" / "windows" / "zs-dev.ps1")
    cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);"
           "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}" % ps)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_launcher_only_kills_its_own_tree_and_profile():
    ps = (REPO / "dev" / "windows" / "zs-dev.ps1").read_text(encoding="utf-8")
    assert "taskkill.exe /PID $s.pid /T /F" in ps               # by recorded PID, never by image name
    for forbidden in ("taskkill /IM", "taskkill.exe /IM", "Stop-Process -Name", "Get-Process chrome", "Remove-Item C:\\", "-ExecutionPolicy Unrestricted"):
        assert forbidden not in ps
