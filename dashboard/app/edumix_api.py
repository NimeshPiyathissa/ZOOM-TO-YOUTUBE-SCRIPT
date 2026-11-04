"""HTTP API + background supervisor for the EduMix source (see app/edumix.py
for what this feature does and deliberately does not do).

Kept out of main.py: main.py only mounts `router`, renders the page and
starts `supervisor.run()` at startup.

Nothing here accepts, stores or logs a phone number, OTP, password or
cookie - the operator types those into the real site over the interactive
preview. Audit entries carry the URL *path* only (a class link's query
string can hold tokens).
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import cdp, config, control, db, deps, edumix
from . import sources as sources_mod
from .env_store import ValidationError

log = logging.getLogger("zoomdash.edumix")
router = APIRouter()

SOURCE_NAME = "EduMix Live"
SETTING_LAST_AUTH = "edumix_last_authenticated_at"
_AUTHENTICATED_FAMILY = {"authenticated", "class_open", "no_live", "player_loading", "buffering",
                         "playing", "paused", "ended", "session_expired"}


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _path_only(url: str) -> str:
    return urlsplit(url or "").path or "/"


def get_edumix_source() -> dict | None:
    for s in sources_mod.list_sources():
        if s["type"] == "edumix":
            return s
    return None


def _active_edumix() -> dict | None:
    s = sources_mod.get_active_source()
    return s if s and s["type"] == "edumix" else None


# ------------------------------------------------------------------ supervisor

class Supervisor:
    """Watches the EduMix page while an edumix source is active:
    * records when the session was last seen valid (a timestamp only),
    * navigates to the pasted class once login completes (once per login
      episode, so it never fights the operator browsing around),
    * reconnects with exponential backoff after a network drop, a page or
      player error, or a browser crash - never touching states a person
      must resolve (sign-in, OTP, expired access, protected playback)."""

    def __init__(self):
        self.reconnector = edumix.Reconnector()
        self.state = "stopped"
        self.last_status: dict | None = None
        self.navigated_this_episode = False
        self.paused = False            # operator pressed Stop: don't resurrect the browser
        self.last_action: dict | None = None

    def mark_started(self) -> None:
        self.paused = False
        self.navigated_this_episode = False
        self.reconnector.reset()

    async def tick(self, now: float | None = None) -> dict | None:
        src = _active_edumix()
        if not src:
            self.state = "stopped"
            return None
        now = time.time() if now is None else now
        st = await edumix.status(self.reconnector)
        prev, state = self.state, st["state"]
        self.state, self.last_status = state, st

        if state in _AUTHENTICATED_FAMILY and state != "session_expired":
            await run_in_threadpool(db.set_setting, SETTING_LAST_AUTH, str(int(now)))
        if state in ("login_required", "awaiting_otp", "session_expired", "browser_down"):
            self.navigated_this_episode = False

        if self.paused:
            return st

        target = _path_only(src["url"])
        if edumix.should_auto_navigate(state, st["page"]["path"], target,
                                       bool((src.get("options") or {}).get("auto_navigate", True)),
                                       self.navigated_this_episode):
            self.navigated_this_episode = True
            try:
                await edumix.navigate_class(src["url"])
                self.last_action = {"action": "auto_navigate", "at": now}
            except (cdp.CDPError, edumix.EduMixError) as exc:
                log.warning("edumix auto-navigate failed: %s", edumix.redact(str(exc)))
            return st

        action = self.reconnector.step(state, now)
        if action == "reload":
            try:
                await edumix.navigate_class(src["url"])
            except (cdp.CDPError, edumix.EduMixError) as exc:
                log.warning("edumix reconnect navigate failed: %s", edumix.redact(str(exc)))
            self.last_action = {"action": "reload", "at": now}
        elif action == "restart_browser":
            try:
                await run_in_threadpool(control.unit_action, "browser-source", "restart")
            except control.ControlError as exc:
                log.warning("edumix browser restart failed: %s", edumix.redact(str(exc)))
            self.last_action = {"action": "restart_browser", "at": now}
        return st

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # never let the loop die
                log.exception("edumix supervisor tick failed")
            await asyncio.sleep(5 if self.state != "stopped" else 15)


supervisor = Supervisor()


# ------------------------------------------------------------------ routes

def _source_view(s: dict | None) -> dict | None:
    if not s:
        return None
    pub = sources_mod.public_view(s)
    opts = pub.get("options") or {}
    return {"id": pub["id"], "name": pub["name"], "url": pub["url"], "path": _path_only(pub["url"]),
            "auto_navigate": bool(opts.get("auto_navigate", True)),
            "rights_confirmed": bool(opts.get("rights_confirmed", False)),
            "active": bool(_active_edumix() and _active_edumix()["id"] == pub["id"])}


@router.get("/api/edumix/status")
async def api_status(request: Request):
    deps.require_session_api(request)
    src = get_edumix_source()
    active = _active_edumix()
    if active:
        st = await edumix.status(supervisor.reconnector)
    else:
        st = edumix.describe("stopped")
        st.update({"capture": edumix.capture_status(), "page": {"path": "", "title": ""}, "player": {"present": False}})
    last = await run_in_threadpool(db.get_setting, SETTING_LAST_AUTH, "")
    st["source"] = _source_view(src)
    st["active"] = bool(active)
    st["paused"] = supervisor.paused
    st["session_last_ok"] = int(last) if str(last).isdigit() else None
    st["last_action"] = supervisor.last_action
    return st


@router.post("/api/edumix/open")
async def api_open(request: Request):
    """Paste a class URL: validate, save/update the single EduMix source,
    make it active. If the EduMix browser is already up in its isolated
    profile, just navigate (session kept, nothing restarts); otherwise
    start it. Whether a login is needed is then read off the page."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "edumix_open", max_calls=6, window_seconds=30)
    body = await request.json()
    try:
        url = edumix.validate_class_url(str(body.get("url") or ""))
    except edumix.EduMixError as exc:
        return _err(str(exc))
    existing = get_edumix_source()
    opts = dict((existing or {}).get("options") or {})
    if "auto_navigate" in body:
        opts["auto_navigate"] = bool(body["auto_navigate"])
    if "rights_confirmed" in body:
        opts["rights_confirmed"] = bool(body["rights_confirmed"])
    try:
        if existing:
            sources_mod.update_source(existing["id"], existing["name"], "edumix", url, opts)
            sid = existing["id"]
        else:
            sid = sources_mod.create_source(SOURCE_NAME, "edumix", url, opts)
    except ValidationError as exc:
        return _err(str(exc))
    src = sources_mod.get_source(sid)

    hot = False
    active = _active_edumix()
    if active and active["id"] == sid:
        try:
            await cdp.probe()
            await edumix.navigate_class(url)
            await run_in_threadpool(control.apply_source_config, src)  # a later restart lands on the same class
            hot = True
        except (cdp.CDPError, control.ControlError):
            hot = False
    outcome: dict = {"hot_swapped": True, "rtmp_dropped": False, "results": []}
    if not hot:
        try:
            outcome = await run_in_threadpool(control.start_source, src)
        except control.ControlError as exc:
            return _err(str(exc), 409)
        sources_mod.set_active_source_id(sid)
    supervisor.mark_started()
    db.audit(session["username"], "edumix_open", _path_only(url), deps.client_ip(request))
    return {"ok": True, "hot_swapped": hot or outcome.get("hot_swapped", False), "source": _source_view(src)}


@router.post("/api/edumix/login")
async def api_login(request: Request):
    """Login / Reauthenticate: put the site's own entry page on the kiosk.
    The operator signs in there by hand (phone number + OTP) - the
    dashboard types nothing."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "edumix_login", max_calls=6, window_seconds=30)
    if not _active_edumix():
        return _err("Open a class first - the EduMix browser isn't running", 409)
    try:
        await edumix.navigate_login()
    except cdp.CDPError as exc:
        return _err(str(exc), 409)
    supervisor.navigated_this_episode = False
    supervisor.mark_started()
    db.audit(session["username"], "edumix_login", "", deps.client_ip(request))
    return {"ok": True}


@router.post("/api/edumix/reconnect")
async def api_reconnect(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "edumix_reconnect", max_calls=6, window_seconds=30)
    src = _active_edumix()
    if not src:
        return _err("The EduMix source isn't active", 409)
    supervisor.mark_started()
    mode = "navigate"
    try:
        await cdp.probe()
        await edumix.navigate_class(src["url"])
    except cdp.CDPError:
        mode = "restart_browser"
        try:
            await run_in_threadpool(control.unit_action, "browser-source", "restart")
        except control.ControlError as exc:
            return _err(str(exc), 409)
    db.audit(session["username"], "edumix_reconnect", mode, deps.client_ip(request))
    return {"ok": True, "mode": mode}


@router.post("/api/edumix/stop")
async def api_stop(request: Request):
    """Stops the EduMix browser. The saved session stays in the isolated
    profile on disk; the supervisor won't restart the browser until the
    operator opens a class again."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "edumix_stop", max_calls=6, window_seconds=30)
    supervisor.paused = True
    try:
        res = await run_in_threadpool(control.unit_action, "browser-source", "stop")
    except control.ControlError as exc:
        return _err(str(exc), 409)
    db.audit(session["username"], "edumix_stop", ip=deps.client_ip(request))
    return {"ok": bool(res.get("ok", True))}


@router.post("/api/edumix/fit")
async def api_fit(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "edumix_fit", max_calls=20, window_seconds=20)
    body = await request.json()
    try:
        return await edumix.set_video_fit(str(body.get("mode") or ""))
    except edumix.EduMixError as exc:
        return _err(str(exc))
    except cdp.CDPError as exc:
        return _err(str(exc), 409)


@router.post("/api/edumix/options")
async def api_options(request: Request):
    """Toggle auto-navigate / the rights acknowledgement on the saved
    source. (rights_confirmed is recorded only; capture stays disabled.)"""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    src = get_edumix_source()
    if not src:
        raise HTTPException(status_code=404, detail="no EduMix source yet")
    body = await request.json()
    opts = dict(src.get("options") or {})
    for key in ("auto_navigate", "rights_confirmed"):
        if key in body:
            opts[key] = bool(body[key])
    try:
        sources_mod.update_source(src["id"], src["name"], "edumix", src["url"], opts)
    except ValidationError as exc:
        return _err(str(exc))
    db.audit(session["username"], "edumix_options",
             f"auto_navigate={opts.get('auto_navigate')} rights_confirmed={opts.get('rights_confirmed')}",
             deps.client_ip(request))
    return {"ok": True, "source": _source_view(sources_mod.get_source(src["id"]))}
