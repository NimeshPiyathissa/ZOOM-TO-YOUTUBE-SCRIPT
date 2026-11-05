"""Local (Windows) development adapter - NOT used by the production deployment.

The dashboard normally drives a Linux box: `sudo -u zoombot systemctl --user ...`, Xvfb :99, x11vnc/noVNC, a
Chrome kiosk. None of that exists on a Windows laptop. Instead of forking the app, `install()` swaps the thin
process layer for local stand-ins, so the SAME dashboard, EduMix page, supervisor and Oxblood UI run unchanged:

* "browser-source" unit  -> a real, visible Chrome window (or Edge) on the desktop, started with its own isolated
  persistent profile and a loopback-only DevTools port. You sign in to the official site in that window yourself.
  Windows encrypts that profile's cookies with your account (DPAPI); nothing here reads them.
* the live view          -> JPEG screenshots of that window over DevTools (`/api/edumix/local/preview.jpg`),
  so Fit / Fill / Fullscreen / Reconnect behave as on the server. The window itself is the interactive surface.
* every other unit       -> simulated "inactive"; `ffmpeg-stream` can never start (capture stays disabled).
* `sudo`                 -> never executed: every `run_as_zoombot` call becomes a harmless no-op.
* all state              -> under one local directory (default %LOCALAPPDATA%\\ZoomToYT-Dev), never the repo.

Nothing here talks to any server, and importing this module changes nothing until `install()` is called.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import re
import socket
import subprocess
import sys
import time

import psutil
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import cdp, config, deps, edumix

log = logging.getLogger("zoomdash.localdev")
router = APIRouter()

_state: dict = {}          # originals saved by install(), restored by uninstall()
browser: "LocalBrowser | None" = None
UNIT_NOTE = "Local development: Linux services are simulated."


# ------------------------------------------------------------------ redaction (logs must never hold secrets)

_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?#]+)\?[^\s\"'<>]*")
_HEADER_RE = re.compile(r"(?i)\b(cookie|set-cookie|authorization|x-csrf-token|proxy-authorization)\b(\s*[:=]\s*)[^\r\n]+")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact_text(text: str) -> str:
    """Masks anything credential-shaped before it can reach a log file: phone numbers, OTP/password/token
    key=value pairs, Cookie/Authorization headers, URL query strings (class links can carry tokens) and long
    token-looking strings."""
    out = edumix.redact(str(text))
    out = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}•••", out)
    out = _URL_QUERY_RE.sub(r"\1?•••", out)
    return _LONG_TOKEN_RE.sub("•••", out)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:                      # a logging filter must never break the app
            record.msg, record.args = "<unprintable log record>", ()
        return True


# ------------------------------------------------------------------ the local browser

def find_chrome() -> str | None:
    """Google Chrome first, then Microsoft Edge (also Chromium). ZS_CHROME_PATH overrides."""
    cands = [os.environ.get("ZS_CHROME_PATH", "")]
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def free_port(start: int, end: int) -> int:
    for p in range(start, end + 1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free localhost port between {start} and {end}")


class LocalBrowser:
    """One visible browser window with an isolated, persistent profile. Every operation is scoped to processes
    whose command line carries OUR --user-data-dir, so it can never touch your everyday Chrome."""

    def __init__(self, chrome: str, profile_dir: pathlib.Path, port: int):
        self.chrome, self.profile_dir, self.port = chrome, pathlib.Path(profile_dir), int(port)
        self.started_at = 0.0

    # Diagnostic extras via ZS_CHROME_FLAGS (e.g. "--headless=new" for unattended checks). Anything that weakens the
    # browser's security or exposes DevTools beyond this machine is refused.
    _DENIED_FLAGS = ("--disable-web-security", "--no-sandbox", "--remote-allow-origins", "--remote-debugging-address",
                     "--remote-debugging-port", "--user-data-dir", "--ignore-certificate-errors", "--allow-running-insecure-content",
                     "--disable-site-isolation", "--enable-automation", "--load-extension", "--host-resolver-rules", "--proxy-server")

    @classmethod
    def extra_flags(cls) -> list[str]:
        out = []
        for f in (os.environ.get("ZS_CHROME_FLAGS") or "").split():
            if not f.startswith("--") or any(f.lower().startswith(d) for d in cls._DENIED_FLAGS):
                raise ValueError(f"ZS_CHROME_FLAGS: flag not allowed: {f}")
            out.append(f)
        return out

    def command(self, url: str) -> list[str]:
        return [
            self.chrome,
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",     # DevTools reachable from this machine only
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", "--lang=en-US",
            "--window-size=1280,860", "--window-position=60,40",
            *self.extra_flags(),
            url,
        ]

    @staticmethod
    def _check_url(url: str) -> str:
        if url == "about:blank":
            return url
        return edumix.validate_class_url(url)           # only mjphysics.lk, https - never an arbitrary page

    def processes(self) -> list[psutil.Process]:
        want = _norm(str(self.profile_dir))
        found = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                for arg in p.info["cmdline"] or []:
                    if arg.startswith("--user-data-dir=") and _norm(arg.split("=", 1)[1].strip('"')) == want:
                        found.append(p)
                        break
            except (psutil.Error, OSError):
                continue
        return found

    def main_process(self) -> psutil.Process | None:
        for p in self.processes():
            try:
                if not any(a.startswith("--type=") for a in p.cmdline()):
                    return p
            except psutil.Error:
                continue
        return None

    def is_running(self) -> bool:
        return self.main_process() is not None

    def phase(self) -> str:
        p = self.main_process()
        if p is None:
            return "STOPPED"
        if port_open(self.port):
            return "LIVE"
        return "STARTING" if time.time() - p.create_time() < 25 else "FAILED"

    def stop(self) -> int:
        procs = self.processes()
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=6)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        return len(procs)

    def start(self, url: str) -> None:
        url = self._check_url(url)
        # A second launch against a profile that is already open would just hand the URL to the old process and
        # exit, silently ignoring the DevTools flags - so close OUR instance first (the session stays on disk).
        self.stop()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(self.command(url), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        self.started_at = time.time()
        log.info("local browser started (profile %s, devtools 127.0.0.1:%s)", self.profile_dir.name, self.port)


# ------------------------------------------------------------------ simulated process layer

def _show(unit: str) -> dict:
    live = False
    phase, up = "STOPPED", None
    if unit == "browser-source" and browser is not None:
        phase = browser.phase()
        live = phase == "LIVE"
        p = browser.main_process()
        up = int(time.time() - p.create_time()) if p is not None else None
    active = "active" if phase in ("LIVE", "STARTING") else "inactive"
    sub = "running" if phase in ("LIVE", "STARTING") else "dead"
    return {"unit": unit, "active_state": active, "sub_state": sub, "result": "success", "restart_count": 0,
            "exec_main_status": "", "exec_main_code": "", "uptime_seconds": up if live else None,
            "main_pid": 1 if live else 0, "unit_file_state": "simulated", "phase": phase}


def _unit_action(unit: str, verb: str) -> dict:
    from . import control
    control._require_unit(unit)
    if verb not in control.VERBS:
        raise control.ControlError(f"unknown verb: {verb}")
    if unit == "ffmpeg-stream" and verb in ("start", "restart"):
        raise control.ControlError(
            "Local development: the encoder is not available here, and EduMix capture / YouTube broadcasting are "
            "disabled until a real live class has been inspected and an authorized capture method is confirmed.")
    if unit == "browser-source" and browser is not None:
        if verb in ("start", "restart"):
            cur = _read_source()
            url = "about:blank"
            if cur.get("SOURCE_TYPE") == "edumix" and cur.get("EDUMIX_URL"):
                url = cur["EDUMIX_URL"]
            browser.start(url)
        elif verb == "stop":
            browser.stop()
    return {"ok": True, "unit": unit, "verb": verb, "stderr": ""}


def _diagnosis() -> dict:
    if browser is None or not browser.is_running():
        return {"connected": False, "code": "unit_down",
                "reason": "The local EduMix browser window isn't running.",
                "fix": "Open a class on the EduMix page (or press Reconnect).", "can_restart": True}
    if not port_open(browser.port):
        return {"connected": False, "code": "starting",
                "reason": "The browser is still starting - its DevTools port comes up a few seconds after launch.",
                "fix": "Wait a few seconds."}
    return {"connected": False, "code": "port_closed",
            "reason": f"The browser is running but DevTools on 127.0.0.1:{browser.port} isn't answering.",
            "fix": "Press Reconnect to restart the local browser.", "can_restart": True}


def _source_file() -> pathlib.Path:
    return pathlib.Path(config.DATA_DIR) / "current-source.json"


def _read_source() -> dict:
    try:
        return json.loads(_source_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_source_values(values: dict) -> None:
    bad = [k for k, v in values.items() if "\n" in str(v)]
    if bad:
        from . import control
        raise control.ControlError(f"value(s) for {', '.join(sorted(bad))} contain a newline, which is not allowed")
    _source_file().parent.mkdir(parents=True, exist_ok=True)
    _source_file().write_text(json.dumps({k: str(v) for k, v in values.items()}), encoding="utf-8")


def _no_sudo(argv, input_bytes=None, timeout=15):
    """Stand-in for control.run_as_zoombot: nothing is ever executed on this machine."""
    log.debug("simulated (not executed): %s", os.path.basename(str(argv[0])) if argv else "?")
    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


# ------------------------------------------------------------------ install / uninstall

def install(data_root: pathlib.Path, chrome_port: int = 9223, chrome_path: str | None = None) -> LocalBrowser:
    """Point every path at `data_root`, simulate the Linux process layer, and create the local browser handle."""
    global browser
    from . import control
    if _state:
        raise RuntimeError("localdev is already installed")
    data_root = pathlib.Path(data_root)
    chrome = chrome_path or find_chrome()
    if not chrome:
        raise RuntimeError("Google Chrome (or Microsoft Edge) was not found. Install Chrome, or set ZS_CHROME_PATH.")

    data = data_root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data_root / "logs").mkdir(parents=True, exist_ok=True)

    saved_cfg = {k: getattr(config, k) for k in (
        "DATA_DIR", "DB_PATH", "OVERLAY_CONFIG_FILE", "BRB_SLATE_FILE", "SECRET_STORE_FILE", "VAULT_LOCK_SENTINEL_FILE",
        "SETTINGS_FILE", "RECORDINGS_DIR", "CHROME_DEBUG_PORT")}
    config.DATA_DIR = data
    config.DB_PATH = data / "dashboard.db"
    config.OVERLAY_CONFIG_FILE = data / "overlay.json"
    config.BRB_SLATE_FILE = data / "brb_slate.json"
    config.SECRET_STORE_FILE = data / "secrets.enc.json"
    config.VAULT_LOCK_SENTINEL_FILE = data / "vault.locked"
    config.SETTINGS_FILE = data / "settings.json"
    config.RECORDINGS_DIR = data_root / "recordings"
    config.CHROME_DEBUG_PORT = int(chrome_port)
    config.LOCAL_DEV = True
    if config.EDUMIX_CAPTURE_ENABLED:
        raise RuntimeError("EDUMIX_CAPTURE_ENABLED must be False in local development")

    browser = LocalBrowser(chrome, data_root / "chrome-profiles" / config.EDUMIX_PROFILE_ID, chrome_port)

    patches = {
        "unit_show": _show,
        "unit_action": _unit_action,
        "read_current_source": _read_source,
        "_write_source_env_values": _write_source_values,
        "browser_diagnosis": _diagnosis,
        "_ffmpeg_is_up": lambda: False,
        "unit_last_error": lambda unit: None,
        "run_as_zoombot": _no_sudo,
    }
    originals: dict = {"cfg": saved_cfg, "attrs": []}
    for modname, mod in list(sys.modules.items()):
        if not (modname == "app" or modname.startswith("app.")) or mod is None:
            continue
        for name, new in patches.items():
            old = getattr(mod, name, None)
            if old is not None and callable(old) and old is not new and (
                    modname == "app.control" or getattr(old, "__module__", "") == "app.control"):
                originals["attrs"].append((mod, name, old))
                setattr(mod, name, new)
    _state.update(originals)
    return browser


def uninstall() -> None:
    """Undo install() (used by tests; the dev server just exits)."""
    global browser
    for mod, name, old in _state.get("attrs", []):
        setattr(mod, name, old)
    for k, v in _state.get("cfg", {}).items():
        setattr(config, k, v)
    config.LOCAL_DEV = False
    _state.clear()
    browser = None


# ------------------------------------------------------------------ live view: screenshots of the real window

_frame: dict = {"at": 0.0, "jpg": None}
FRAME_TTL = 0.45


async def capture_jpeg(quality: int = 55) -> bytes:
    """One JPEG of the browser tab via DevTools. Shared for FRAME_TTL so many open tabs cost one capture.
    Held in memory only, never written to disk or logged - it can show what you type into the site."""
    now = time.monotonic()
    if _frame["jpg"] and now - _frame["at"] < FRAME_TTL:
        return _frame["jpg"]
    import websockets
    target = await cdp._get_page_target()
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=32 * 2 ** 20, open_timeout=5) as ws:
        res = await asyncio.wait_for(
            cdp._call(ws, 1, "Page.captureScreenshot", {"format": "jpeg", "quality": int(quality), "fromSurface": True}),
            timeout=8)
    jpg = base64.b64decode(res["data"])
    _frame["at"], _frame["jpg"] = time.monotonic(), jpg
    return jpg


async def bring_to_front() -> None:
    import websockets
    target = await cdp._get_page_target()
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=2 ** 20, open_timeout=5) as ws:
        await cdp._call(ws, 1, "Page.bringToFront")


@router.get("/api/edumix/local/preview.jpg")
async def api_preview(request: Request):
    deps.require_session_api(request)
    try:
        jpg = await capture_jpeg()
    except Exception as exc:                 # any capture failure simply means "no frame" - the page retries
        return JSONResponse({"error": "no frame: " + redact_text(str(exc))[:120]}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/api/edumix/local/focus")
async def api_focus(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "edumix_local_focus", max_calls=10, window_seconds=20)
    try:
        await bring_to_front()
    except Exception as exc:
        return JSONResponse({"error": redact_text(str(exc))[:160]}, status_code=409)
    return {"ok": True}
