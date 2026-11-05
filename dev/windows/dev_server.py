"""Windows local-development entry point for the dashboard + EduMix page.

Runs the SAME application from this repository (feature/edumix-live) on 127.0.0.1, with the Linux-only process
layer replaced by the local adapter in dashboard/app/localdev.py. Driven by dev/windows/zs-dev.ps1 - run that
instead of this file directly.

    --serve          run the dashboard (default)
    --init-admin     create the local admin user if missing and print its generated password ONCE (--reset to redo)
    --stop-browser   close the local EduMix browser window (only the one using the isolated profile)
    --doctor         print redacted diagnostics
Nothing here contacts any server, and no production setting is read or written.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import pathlib
import secrets
import sys
import time
import types

REPO = pathlib.Path(__file__).resolve().parents[2]
DASH = REPO / "dashboard"
DEFAULT_PORT_RANGE = (8765, 8799)


def dev_home() -> pathlib.Path:
    base = os.environ.get("ZS_DEV_HOME") or os.path.join(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home())), "ZoomToYT-Dev")
    return pathlib.Path(base)


def _stub_posix_modules() -> None:
    """control.py imports pwd/grp (POSIX only). They are only used to look up the zoombot uid on a real VPS."""
    for name, attr in (("pwd", "getpwnam"), ("grp", "getgrnam")):
        if name not in sys.modules:
            try:
                __import__(name)
            except ImportError:
                m = types.ModuleType(name)
                setattr(m, attr, lambda n: types.SimpleNamespace(pw_uid=1000, gr_gid=1000))
                sys.modules[name] = m


def _bootstrap(chrome_port: int):
    """Stub POSIX modules, import the app config, redirect every path into the dev home, install the adapter."""
    _stub_posix_modules()
    sys.path.insert(0, str(DASH))
    home = dev_home()
    for sub in ("data", "logs", "run"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    from app import config, localdev          # noqa: E402  (after sys.path / stubs)
    browser = localdev.install(home, chrome_port=chrome_port)
    return home, config, localdev, browser


def _setup_logging(home: pathlib.Path, localdev) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(home / "logs" / "dashboard.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    h.addFilter(localdev.RedactingFilter())        # nothing credential-shaped can reach the log file
    root.addHandler(h)
    for noisy in ("apscheduler", "httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def _admin(home: pathlib.Path, reset: bool) -> int:
    from app import db, security
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if row and not reset:
            print("admin user already exists (use 'reset-password' to generate a new password)")
            return 0
        pw = secrets.token_urlsafe(12)
        if row:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(pw), row["id"]))
            if _has_col(conn, "sessions", "user_id"):
                conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))     # sign out old dashboard sessions
        else:
            conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                         ("admin", security.hash_password(pw), time.time()))
    bar = "=" * 62
    print(bar)
    print("  Local dashboard login (shown ONCE - only its hash is stored)")
    print(f"     username: admin")
    print(f"     password: {pw}")
    print(bar)
    return 0


def _has_col(conn, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _serve(port: int, chrome_port: int) -> int:
    home, config, localdev, browser = _bootstrap(chrome_port)
    _setup_logging(home, localdev)
    log = logging.getLogger("zoomdash.dev")
    if port == 0:
        port = localdev.free_port(*DEFAULT_PORT_RANGE)
    if localdev.port_open(chrome_port):
        # Something already answers on the DevTools port and it is not ours - never attach to an unknown browser.
        if not browser.is_running():
            print(f"DevTools port {chrome_port} is already in use by another program. Use -ChromePort to pick another.", file=sys.stderr)
            return 3
    from app import db, main as m
    db.init_db()
    m.app.include_router(localdev.router)
    m.templates.env.globals["local_dev"] = True
    orig_static = m._static_url

    def fresh_static(path):                      # edits to static files show up on reload (no stale content hash)
        m._static_hash_cache.pop(path, None)
        return orig_static(path)
    m.templates.env.globals["static_url"] = fresh_static

    state = home / "run" / "state.json"
    state.write_text(json.dumps({"pid": os.getpid(), "port": port, "chrome_port": chrome_port,
                                 "url": f"http://127.0.0.1:{port}/edumix", "started": time.time()}), encoding="utf-8")
    log.info("local dashboard starting on 127.0.0.1:%s (capture enabled=%s, local_dev=%s)", port, config.EDUMIX_CAPTURE_ENABLED, config.LOCAL_DEV)
    import atexit
    atexit.register(lambda: (state.unlink(missing_ok=True), browser.stop()))
    import uvicorn
    try:
        uvicorn.run(m.app, host="127.0.0.1", port=port, log_config=None, access_log=False, ws_ping_interval=20, ws_ping_timeout=60)
    finally:
        state.unlink(missing_ok=True)
    return 0


def _doctor(chrome_port: int) -> int:
    home, config, localdev, browser = _bootstrap(chrome_port)
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'OK' if good else '!!'}] {label}" + (f" - {detail}" if detail else ""))

    print(f"Python {sys.version.split()[0]} | repo {REPO}")
    line(sys.version_info >= (3, 10), "Python >= 3.10")
    for mod in ("fastapi", "uvicorn", "httpx", "websockets", "psutil", "argon2", "apscheduler", "pyDes"):
        try:
            __import__(mod); line(True, f"import {mod}")
        except Exception as exc:
            line(False, f"import {mod}", str(exc)[:80])
    line(bool(browser.chrome), "Chrome/Edge found", browser.chrome)
    line(config.LOCAL_DEV is True, "local-dev adapter active")
    line(config.EDUMIX_CAPTURE_ENABLED is False, "EduMix capture disabled (EDUMIX_CAPTURE_ENABLED=False)")
    line(str(config.DATA_DIR).startswith(str(home)), "all data under the dev home", str(home))
    line(not str(config.DATA_DIR).startswith(str(REPO)), "no data inside the git repository")
    st = home / "run" / "state.json"
    if st.exists():
        s = json.loads(st.read_text(encoding="utf-8"))
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{s['port']}/health", timeout=3).read()
            line(True, "dashboard answering", f"http://127.0.0.1:{s['port']}/edumix")
        except Exception as exc:
            line(False, "dashboard answering", str(exc)[:80])
    else:
        print("  [--] dashboard not running (zs-dev start)")
    prof = browser.profile_dir
    print(f"  [--] browser profile: {prof} ({'exists' if prof.exists() else 'not created yet'}; Windows-encrypted cookies, never read by this app)")
    print(f"  [--] browser: {browser.phase()} | DevTools 127.0.0.1:{chrome_port} {'open' if localdev.port_open(chrome_port) else 'closed'}")
    print(f"  [--] log: {home / 'logs' / 'dashboard.log'}")
    print("RESULT:", "all good" if ok else "problems found (see [!!] lines)")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--init-admin", action="store_true")
    mode.add_argument("--stop-browser", action="store_true")
    mode.add_argument("--doctor", action="store_true")
    ap.add_argument("--reset", action="store_true", help="with --init-admin: generate a new password")
    ap.add_argument("--port", type=int, default=0, help="dashboard port (0 = first free from 8765)")
    ap.add_argument("--chrome-port", type=int, default=9223, help="DevTools port of the local browser (127.0.0.1 only)")
    a = ap.parse_args()
    if a.init_admin:
        _bootstrap(a.chrome_port)
        return _admin(dev_home(), a.reset)
    if a.stop_browser:
        _, _, _, browser = _bootstrap(a.chrome_port)
        print(f"closed {browser.stop()} browser process(es) of the EduMix profile")
        return 0
    if a.doctor:
        return _doctor(a.chrome_port)
    return _serve(a.port, a.chrome_port)


if __name__ == "__main__":
    sys.exit(main())
