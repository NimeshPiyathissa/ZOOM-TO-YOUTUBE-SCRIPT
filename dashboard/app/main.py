from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import subprocess
import time

from fastapi import FastAPI, Request, Response, HTTPException, WebSocket
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, FileResponse, PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import config, db, security, deps, control, env_store, profiles as profiles_mod
from . import logs as logs_mod
from . import preview, stats, scheduler, vnc_proxy, vncauth
from . import sources as sources_mod
from . import probe as probe_mod
from . import url_security
from .url_security import URLSecurityError
from . import zoomlink
from . import cdp, youtube, youtube_watch
from .youtube import YouTubeURLError
from . import accounts as accounts_mod
from .accounts import AccountError
from . import audio_level
from . import recording, settings_store, telegram

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
logger = logging.getLogger("zoom-stream.main")

app = FastAPI(title="Zoom Stream Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------- helpers

def _is_https(request: Request) -> bool:
    if not config.TRUST_FORWARDED_PROTO:
        return True  # this process terminates TLS itself
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def _set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(
        config.COOKIE_NAME, session_id,
        max_age=config.SESSION_ABSOLUTE_TIMEOUT_SECONDS,
        httponly=True, samesite="strict", secure=_is_https(request), path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(config.COOKIE_NAME, path="/")


def _require_page(request: Request):
    """Returns a session dict, or a RedirectResponse to /login to return
    directly from the route handler."""
    session = deps.get_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    return session


def _api_error(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # noVNC is vendored under /static/vendor/novnc, so no third-party
        # script origin is needed any more (was: https://cdn.jsdelivr.net).
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        # blob: is required for the Overview preview thumbnail (overview.js
        # fetches a JPEG and shows it via URL.createObjectURL), a JS-created
        # same-origin object URL, not attacker-controllable remote content.
        # i.ytimg.com: YouTube's public thumbnail CDN for the saved-links
        # library (app/youtube.py thumbnail_url) - images only.
        "img-src 'self' data: blob: https://i.ytimg.com; "
        "connect-src 'self' ws: wss:; "
        "frame-src 'self' https://www.youtube.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


# ---------------------------------------------------------------- auth

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if deps.get_session(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    ip = deps.client_ip(request)

    if security.is_locked_out(ip, username):
        db.audit(username, "login_locked_out", ip=ip)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again in 15 minutes."},
            status_code=429,
        )

    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    ok = bool(row) and security.verify_password(password, row["password_hash"])
    security.record_login_attempt(ip, username, ok)
    if not ok:
        db.audit(username, "login_failed", ip=ip)
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid username or password."}, status_code=401
        )

    security.clear_login_failures(ip, username)
    session_id, _csrf = security.create_session(row["id"], ip)
    db.audit(username, "login_success", ip=ip)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, session_id)
    return resp


@app.post("/logout")
async def logout(request: Request):
    session_id = request.cookies.get(config.COOKIE_NAME)
    if session_id:
        security.destroy_session(session_id)
    resp = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(resp)
    return resp


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
async def overview_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    return templates.TemplateResponse("overview.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "units": config.VISIBLE_UNITS,
    })


@app.get("/controls", response_class=HTMLResponse)
async def controls_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    active = sources_mod.get_active_source()
    return templates.TemplateResponse("controls.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "units": config.VISIBLE_UNITS, "sources": sources_mod.list_sources_public(),
        "active_source_id": active["id"] if active else None,
    })


@app.get("/remote", response_class=HTMLResponse)
async def remote_page(request: Request):
    """Touch-first quick-control surface (Part 3) - a separate page from
    /controls (the full admin settings page) on purpose: this is the
    small set of things worth doing one-handed from a phone while away
    from the desk, not the whole pipeline/danger-zone surface."""
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    links = youtube_watch.list_links()
    active = sources_mod.get_active_source()
    return templates.TemplateResponse("remote.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "youtube_links": links, "youtube_settings": youtube_watch.get_settings(),
        "sources": sources_mod.list_sources_public(),
        "active_source_id": active["id"] if active else None,
        "sources_rev": sources_mod.sources_rev(),
        "accounts": accounts_mod.list_accounts(),
    })


@app.get("/zoom", response_class=HTMLResponse)
async def zoom_page(request: Request):
    """The Zoom page: every way of joining a meeting, the live meeting
    state, and the saved-meetings library - which is simply the zoom-type
    rows of the one `sources` table /remote also lists."""
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    active = sources_mod.get_active_source()
    meetings = [s for s in sources_mod.list_sources_public() if s["type"] == "zoom"]
    return templates.TemplateResponse("zoom.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "meetings": meetings, "active_source_id": active["id"] if active else None,
        "sources_rev": sources_mod.sources_rev(),
        "accounts": accounts_mod.list_accounts(), "timezone": config.TIMEZONE,
    })


@app.get("/studio", response_class=HTMLResponse)
async def studio_page(request: Request):
    """Dedicated YouTube Live Studio Room: broadcast-grade master controls,
    real-time tally strip, stream health telemetry, dual-channel audio peak ladder,
    stream key / RTMP manager, source indicator, and mobile slide-over live chat."""
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    active = sources_mod.get_active_source()
    parsed_env = env_store.read_parsed()
    yt_key = parsed_env.get("YT_STREAM_KEY", "")
    masked_key_hint = f"••••••••••••{yt_key[-4:]}" if len(yt_key) >= 4 else ("Configured" if yt_key else "Not configured")

    studio_settings = {
        "video_id": db.get_setting("yt_studio_video_id", "") or "",
        "watch_url": db.get_setting("yt_studio_watch_url", "") or "",
        "control_room_url": db.get_setting("yt_studio_control_room_url", "https://studio.youtube.com/channel/live/livestreaming") or "https://studio.youtube.com/channel/live/livestreaming",
    }

    return templates.TemplateResponse("studio.html", {
        "request": request,
        "csrf_token": session["csrf_token"],
        "username": session["username"],
        "active_source": active,
        "stream_key_configured": bool(yt_key),
        "stream_key_hint": masked_key_hint,
        "rtmp_ingest_url": "rtmps://a.rtmps.youtube.com:443/live2",
        "studio_settings": studio_settings,
        "sources_rev": sources_mod.sources_rev(),
    })


@app.get("/overlay", response_class=HTMLResponse)
async def overlay_page(request: Request):
    """Zero-CPU OBS-Style Text Overlay Studio: interactive 16:9 canvas preview,
    draggable text positioning, snap-to-corner anchors, rich typography engine
    with 15+ Google Fonts, font styling, outline/stroke, background box, and drop shadow."""
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    from . import overlay
    state = overlay.get_overlay_state()
    return templates.TemplateResponse("overlay.html", {
        "request": request,
        "csrf_token": session["csrf_token"],
        "username": session["username"],
        "overlay_state": state,
        "google_fonts": overlay.GOOGLE_FONTS,
        "font_categories": overlay.GOOGLE_FONT_CATEGORIES,
        "font_preview_urls": overlay.get_google_fonts_preview_urls(),
    })


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    return templates.TemplateResponse("accounts.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "accounts": accounts_mod.list_accounts(),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_alias_page(request: Request):
    return await config_page(request)


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    active = sources_mod.get_active_source()
    zoom_account = {
        "label": db.get_setting("zoom_account_label", ""),
        "confirmed_signed_in": db.get_setting("zoom_account_signed_in", "0") == "1",
    }
    return templates.TemplateResponse("config.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "cfg": env_store.masked_view(), "sources": sources_mod.list_sources_public(),
        "active_source_id": active["id"] if active else None,
        "presets": config.RESOLUTION_PRESETS,
        "auto_recovery": db.get_setting("auto_recovery", "off"),
        "webhook_configured": bool(db.get_setting("webhook_url", "")),
        "zoom_account": zoom_account,
        "signin_modes": sorted(config.ZOOM_SIGNIN_MODES),
        "direct_modes": sorted(config.DIRECT_MODES),
        "accounts": accounts_mod.list_accounts(),
        "telegram_cfg": settings_store.masked_settings(),
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    return templates.TemplateResponse("logs.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "units": config.VISIBLE_UNITS, "errors": logs_mod.recent_errors(),
    })


@app.get("/vnc", response_class=HTMLResponse)
async def vnc_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    vnc_pass = vncauth.current_password()
    return templates.TemplateResponse(
        "vnc.html",
        {"request": request, "username": session["username"], "vnc_password": vnc_pass},
    )


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    return templates.TemplateResponse("schedule.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "sources": sources_mod.list_sources_public(), "schedules": _list_schedules(),
    })


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    return templates.TemplateResponse("audit.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "rows": [dict(r) for r in rows],
    })


# ---------------------------------------------------------------- api: state / preview

def _truncate_url(url: str, head: int = 40, tail: int = 12) -> str:
    if len(url) <= head + tail + 1:
        return url
    return f"{url[:head]}…{url[-tail:]}"


@app.get("/api/state")
async def api_state(request: Request):
    deps.require_session_api(request)
    units = []
    stream_show = None
    for unit in config.VISIBLE_UNITS:
        try:
            show = control.unit_show(unit)
        except control.ControlError as exc:
            show = {
                "unit": unit, "active_state": "unknown", "sub_state": "",
                "phase": control.PHASE_STOPPED, "main_pid": 0, "error": str(exc),
            }
        units.append(show)
        # Fetched exactly once, reused for both the hero/top-badge summary
        # below and this same unit's row in `units` - see control.py's
        # module note on the disagreement bug this fixes.
        if unit == "ffmpeg-stream":
            stream_show = show

    active_source = sources_mod.get_active_source()
    active_source_view = None
    source_health = None
    if active_source:
        active_source_view = {
            "id": active_source["id"], "name": active_source["name"], "type": active_source["type"],
            "url": zoomlink.redact_url(active_source["url"]),
            "url_truncated": _truncate_url(zoomlink.redact_url(active_source["url"])),
        }
        if active_source["type"] == "webpage":
            source_health = stats.webpage_health()

    return {
        "stream": stats.stream_state(stream_show),
        "ffmpeg": stats.ffmpeg_progress(stream_show),
        "system": stats.system_stats(),
        "units": units,
        "active_source": active_source_view,
        "producer_unit": control.producer_unit_for(active_source),
        "source_health": source_health,
        "server_time": time.time(),
    }


@app.get("/api/preview.jpg")
async def api_preview(request: Request):
    deps.require_session_api(request)
    await preview.manager.touch()
    data = preview.manager.latest_bytes()
    if not data:
        raise HTTPException(status_code=404, detail="no preview available yet")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- api: unit / pipeline / stream control

@app.post("/api/units/{unit}/{verb}")
async def api_unit_action(unit: str, verb: str, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    if verb not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="invalid verb")
    try:
        result = control.unit_action(unit, verb)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], f"unit_{verb}", unit, deps.client_ip(request))
    return result


@app.post("/api/pipeline/restart")
async def api_pipeline_restart(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    results = await run_in_threadpool(control.pipeline_restart)
    db.audit(session["username"], "pipeline_restart", ip=deps.client_ip(request))
    return {"results": results}


@app.post("/api/stream/{action}")
async def api_stream_action(action: str, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "stream_" + action, max_calls=6, window_seconds=15)
    verb = {"go-live": "start", "stop": "stop", "restart": "restart"}.get(action)
    if not verb:
        raise HTTPException(status_code=400, detail="invalid action")
    try:
        result = control.unit_action("ffmpeg-stream", verb)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], f"stream_{action}", ip=deps.client_ip(request))
    return result


def _schedule_zoom_followup(source: dict, join_via: str = "client") -> None:
    """Exactly one background follow-up after a zoom source becomes
    active/joined, depending on the path: the desktop client gets
    _apply_join_options_later (mic/camera/view, unchanged); a direct web
    join gets _web_join_followup (drives Zoom's pre-join screen to
    actually reach the meeting); join_method "auto" gets
    _auto_fallback_watcher (watches the client attempt and falls back to
    web only on a mechanism-level failure - see its docstring for exactly
    which failures those are). Shared by the dedicated Join/Rejoin
    buttons (_zoom_action) and the generic source-switch endpoint
    (api_sources_switch), which is also how /remote and the scheduler
    activate a zoom source."""
    method = (source.get("options") or {}).get("join_method", "client")
    if method == "auto":
        asyncio.create_task(_auto_fallback_watcher(source["id"]))
    elif join_via == "web":
        asyncio.create_task(_web_join_followup(source["id"]))
    else:
        asyncio.create_task(_apply_join_options_later(source["id"]))


async def _zoom_action(action: str, request: Request):
    """join / rejoin re-apply the active Zoom source's config first (so a
    fresh ZOOM_JOIN_EPOCH resets the rejoin counter), then start/restart
    whichever producer join_method resolves to; leave stops it. After a
    join, exactly one background follow-up is scheduled - see
    _schedule_zoom_followup."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_" + action, max_calls=6, window_seconds=15)
    active = sources_mod.get_active_source()
    try:
        result = await run_in_threadpool(control.zoom_join, action, active)
    except control.ControlError as exc:
        return _api_error(exc)
    if action in ("join", "rejoin") and active:
        sources_mod.mark_joined(active["id"])
        _schedule_zoom_followup(active, result.get("join_via", "client"))
    db.audit(session["username"], f"zoom_{action}", active["name"] if active else "", deps.client_ip(request))
    return result


@app.post("/api/zoom/join")
async def api_zoom_join(request: Request):
    return await _zoom_action("join", request)


@app.post("/api/zoom/leave")
async def api_zoom_leave(request: Request):
    return await _zoom_action("leave", request)


@app.post("/api/zoom/rejoin")
async def api_zoom_rejoin(request: Request):
    return await _zoom_action("rejoin", request)


_join_options_last: dict = {}


async def _apply_join_options_later(source_id: int, timeout: float = 150.0) -> None:
    """Poll for the meeting window after a join, then enforce the saved
    mic/camera/view options once. Result is kept for the Zoom page to
    show (GET /api/zoom/join-options)."""
    deadline = time.time() + timeout
    _join_options_last.clear()
    _join_options_last.update({"source_id": source_id, "state": "waiting", "started_at": time.time()})
    try:
        while time.time() < deadline:
            await asyncio.sleep(4)
            st = await run_in_threadpool(control.zoom_meeting_status)
            if st.get("status") == "in_meeting":
                s = sources_mod.get_source(source_id)
                if not s:
                    break
                report = await run_in_threadpool(control.zoom_apply_join_options, s)
                _join_options_last.update({"state": "done", "report": report, "finished_at": time.time()})
                db.audit(None, "zoom_join_options_applied",
                         f"source_id={source_id} applied={','.join(report.get('applied', []))} skipped={','.join(report.get('skipped', []))}")
                return
            if st.get("terminal") or (st.get("status") == "not_joined" and st.get("service") == control.PHASE_STOPPED):
                _join_options_last.update({"state": "abandoned", "status": st.get("status"), "finished_at": time.time()})
                return
        _join_options_last.update({"state": "timeout", "finished_at": time.time()})
    except Exception as exc:  # noqa: BLE001 - background task must not die silently
        _join_options_last.update({"state": "error", "error": str(exc)[:200], "finished_at": time.time()})


async def _web_join_followup(source_id: int, timeout: float = 75.0) -> None:
    """join_method=web: browser-source.sh has already navigated the kiosk
    to the wc/join URL by the time this runs; drives Zoom's pre-join
    screen the rest of the way (zoom_web.join_from_browser) and records
    which path was used. Shares _join_options_last with the desktop
    follow-up so the Zoom page only has to poll one endpoint regardless
    of path."""
    from . import zoom_web
    _join_options_last.clear()
    _join_options_last.update({"source_id": source_id, "state": "web_joining", "started_at": time.time()})
    try:
        s = sources_mod.get_source(source_id)
        if not s:
            _join_options_last.update({"state": "abandoned", "finished_at": time.time()})
            return
        options = s.get("options") or {}
        result = await run_in_threadpool(
            zoom_web.join_from_browser, options.get("bot_name", "Stream Bot"), options.get("passcode") or None, timeout,
        )
        await run_in_threadpool(sources_mod.set_last_join_method, source_id, "web")
        db.audit(None, "zoom_web_join",
                 f"source_id={source_id} ok={result.get('ok')} status={(result.get('status') or {}).get('status')}")
        _join_options_last.update({"state": "done" if result.get("ok") else "web_failed",
                                   "result": result, "finished_at": time.time()})
    except Exception as exc:  # noqa: BLE001 - background task must not die silently
        _join_options_last.update({"state": "error", "error": str(exc)[:200], "finished_at": time.time()})


async def _auto_fallback_watcher(source_id: int, client_timeout: float = 40.0) -> None:
    """join_method=auto: the join already started on the desktop client
    (control.zoom_join resolves auto to "client" for the first attempt -
    see producer_unit_for). Watches zoom_meeting_status() for
    `client_timeout` seconds. Falls back to the web client only when the
    client attempt produced NO verifiable signal at all in that window -
    never reached in_meeting, never reached any terminal status either -
    which means the desktop client itself failed to get off the ground
    (crash-looping, or launched but never produced a readable window).
    Any terminal status (passcode/expired/locked/registration_required/
    removed/waiting_room/join_failed/...) is a property of the meeting
    itself, not the join mechanism, so it is NOT a fallback trigger - the
    web client would hit the identical wall and a fallback there would
    just burn a join attempt for nothing."""
    from . import zoom_web
    _join_options_last.clear()
    _join_options_last.update({"source_id": source_id, "state": "watching_client", "started_at": time.time()})
    try:
        deadline = time.time() + client_timeout
        while time.time() < deadline:
            await asyncio.sleep(4)
            st = await run_in_threadpool(control.zoom_meeting_status)
            if st.get("status") == "in_meeting":
                s = sources_mod.get_source(source_id)
                await run_in_threadpool(sources_mod.set_last_join_method, source_id, "client")
                if s:
                    report = await run_in_threadpool(control.zoom_apply_join_options, s)
                    _join_options_last.update({"state": "done", "report": report, "finished_at": time.time()})
                return
            if st.get("terminal"):
                await run_in_threadpool(sources_mod.set_last_join_method, source_id, "client")
                _join_options_last.update({"state": "abandoned", "status": st.get("status"), "finished_at": time.time()})
                return

        # No signal at all within the window - fall back to the web client,
        # continuing the same operator-initiated join (switch_zoom_join_via
        # deliberately doesn't touch ZOOM_JOIN_EPOCH/rejoin counters).
        s = sources_mod.get_source(source_id)
        if not s:
            _join_options_last.update({"state": "abandoned", "reason": "source no longer exists", "finished_at": time.time()})
            return
        _join_options_last.update({"state": "falling_back_to_web"})
        options = s.get("options") or {}
        await run_in_threadpool(control.switch_zoom_join_via, "web")
        await run_in_threadpool(control.unit_action, "zoom", "stop")
        await run_in_threadpool(control.unit_action, "browser-source", "restart")
        result = await run_in_threadpool(
            zoom_web.join_from_browser, options.get("bot_name", "Stream Bot"), options.get("passcode") or None,
        )
        await run_in_threadpool(sources_mod.set_last_join_method, source_id, "web")
        db.audit(None, "zoom_auto_fallback",
                 f"source_id={source_id} web_ok={result.get('ok')} status={(result.get('status') or {}).get('status')}")
        _join_options_last.update({"state": "done" if result.get("ok") else "web_failed",
                                   "result": result, "fell_back": True, "finished_at": time.time()})
    except Exception as exc:  # noqa: BLE001 - background task must not die silently
        _join_options_last.update({"state": "error", "error": str(exc)[:200], "finished_at": time.time()})


@app.get("/api/zoom/join-options")
async def api_zoom_join_options_last(request: Request):
    deps.require_session_api(request)
    return dict(_join_options_last)


@app.post("/api/zoom/apply-options")
async def api_zoom_apply_options(request: Request):
    """Enforce the active meeting's saved mic/camera/view now (only
    toggles a control whose read-back state differs; unknown = left
    alone and reported)."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_apply_options", max_calls=6, window_seconds=30)
    active = sources_mod.get_active_source()
    if not active or active["type"] != "zoom":
        raise HTTPException(status_code=400, detail="the active source isn't a Zoom meeting")
    report = await run_in_threadpool(control.zoom_apply_join_options, active)
    db.audit(session["username"], "zoom_apply_options",
             f"applied={','.join(report.get('applied', []))} skipped={','.join(report.get('skipped', []))}", deps.client_ip(request))
    return report


@app.post("/api/zoom/reset")
async def api_zoom_reset(request: Request):
    """Reset Zoom window: dismiss stale dialogs, and if Zoom is still
    stuck, quit it to the slate. The encoder is never touched."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_reset", max_calls=4, window_seconds=30)
    try:
        result = await run_in_threadpool(control.zoom_reset)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_reset",
             f"dismissed={len(result.get('dismissed', []))} stopped={result.get('stopped')}", deps.client_ip(request))
    return result


@app.post("/api/zoom/clean-feed")
async def api_zoom_clean_feed_post(request: Request):
    """Instant one-click clean-feed trigger: forces zero-controls and removes black borders over CDP."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_clean_feed", max_calls=20, window_seconds=30)
    from . import zoom_web, telegram
    result = await zoom_web.apply_cleanfeed_async()
    db.audit(session["username"], "zoom_clean_feed", f"ok={result.get('ok')}", deps.client_ip(request))
    if result.get("ok"):
        telegram.alert_clean_feed_triggered()
    return result


@app.get("/api/zoom/clean-feed")
async def api_zoom_clean_feed_get(request: Request):
    """GET variant of clean-feed trigger for quick verification and manual call."""
    session = deps.require_session_api(request)
    from . import zoom_web, telegram
    result = await zoom_web.apply_cleanfeed_async()
    db.audit(session["username"], "zoom_clean_feed", f"ok={result.get('ok')}", deps.client_ip(request))
    if result.get("ok"):
        telegram.alert_clean_feed_triggered()
    return result


@app.post("/api/zoom/parse")
async def api_zoom_parse(request: Request):
    """Smart paste: whatever was pasted (link, personal link, registration
    page, meeting ID, personal room URL, zoommtg:// deep link, or a whole
    invite) -> what was understood, with warnings, for the operator to
    confirm or correct. Secrets come back redacted (url_redacted,
    has_passcode); the raw url/passcode are echoed only so the form can
    submit them straight back to POST /api/sources over the same session.
    Personal rooms are resolved through Zoom's own redirect."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_parse", max_calls=20, window_seconds=30)
    body = await request.json()
    text = str(body.get("text", ""))[:20000]
    passcode = str(body.get("passcode", "") or "")[:128]
    parsed = zoomlink.parse_any(text, passcode)
    if parsed.get("needs_resolve") and parsed.get("url"):
        try:
            r = await run_in_threadpool(zoomlink.resolve_vanity, parsed["url"])
            parsed.update({"vanity_url": parsed["url"], "url": r["url"], "meeting_id": r["meeting_id"],
                           "meeting_id_formatted": zoomlink.format_meeting_id(r["meeting_id"]),
                           "url_redacted": zoomlink.redact_url(r["url"]), "needs_resolve": False,
                           "has_passcode": bool(parsed.get("passcode")) or r["has_pwd"], "ok": True})
            parsed["warnings"].append("Personal room resolved to its current meeting ID. If the host changes the room's PMI, re-paste the room URL.")
        except (zoomlink.ZoomLinkError, Exception) as exc:  # network errors included
            parsed["errors"].append(str(exc)[:300] if isinstance(exc, zoomlink.ZoomLinkError) else "Couldn't reach zoom.us to resolve the room right now.")
            parsed["ok"] = False
    db.audit(session["username"], "zoom_parse", f"{parsed.get('input_kind')} ok={parsed.get('ok')}", deps.client_ip(request))
    return parsed


@app.post("/api/test-recording")
async def api_test_recording(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    result = await run_in_threadpool(control.run_test_recording)
    db.audit(session["username"], "test_recording", ip=deps.client_ip(request))
    return result


@app.get("/api/test-recording/download")
async def api_test_recording_download(request: Request):
    deps.require_session_api(request)
    if not config.LATEST_TEST_RECORDING.exists():
        raise HTTPException(status_code=404, detail="no test recording yet")
    return FileResponse(config.LATEST_TEST_RECORDING, filename="test-recording.mp4", media_type="video/mp4")


@app.post("/api/reboot")
async def api_reboot(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    if body.get("confirm") != "REBOOT":
        raise HTTPException(status_code=400, detail="confirmation token required")
    db.audit(session["username"], "reboot_vps", ip=deps.client_ip(request))
    await run_in_threadpool(control.reboot_vps)
    return {"ok": True}


# ---------------------------------------------------------------- api: config

@app.get("/api/config")
async def api_config_get(request: Request):
    deps.require_session_api(request)
    return env_store.masked_view()


@app.post("/api/config")
async def api_config_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    updates = await request.json()
    try:
        units = await run_in_threadpool(env_store.write_updates, updates)
    except (env_store.ValidationError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "config_update", ",".join(sorted(updates.keys())), deps.client_ip(request))
    return {"units_to_restart": units}


@app.post("/api/config/apply-restarts")
async def api_config_apply_restarts(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    units = body.get("units", [])
    if set(units) == set(config.UNIT_ORDER):
        results = await run_in_threadpool(control.pipeline_restart)
    else:
        bad = [u for u in units if u not in config.ALLOWED_UNITS]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown units: {bad}")
        results = [control.unit_action(u, "restart") for u in units]
    db.audit(session["username"], "apply_restarts", ",".join(units), deps.client_ip(request))
    return {"results": results}


@app.post("/api/config/vnc-password")
async def api_config_vnc_password(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    new_password = str(body.get("new_password", ""))
    try:
        result = await run_in_threadpool(control.rotate_vnc_password, new_password)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "vnc_password_rotated", ip=deps.client_ip(request))
    return result


@app.post("/api/config/extract-zoom-link")
async def api_extract_zoom_link(request: Request):
    deps.require_session_api(request)
    body = await request.json()
    meeting_id, passcode = env_store.extract_zoom_id_passcode(str(body.get("link", "")))
    return {"meeting_id": meeting_id, "passcode": passcode}


# ---------------------------------------------------------------- api: studio

@app.get("/api/studio/stream-key/reveal")
async def api_studio_stream_key_reveal(request: Request):
    """The YouTube RTMP Ingest URL and Stream Key for an explicit reveal/copy tap.
    Rate-limited and audited; never cached."""
    session = deps.require_session_api(request)
    deps.require_rate_limit(session, "studio_key_reveal", max_calls=10, window_seconds=60)
    parsed_env = env_store.read_parsed()
    yt_key = parsed_env.get("YT_STREAM_KEY", "")
    db.audit(session["username"], "studio_key_reveal", "", deps.client_ip(request))
    return JSONResponse({
        "stream_key": yt_key,
        "rtmp_url": "rtmps://a.rtmps.youtube.com:443/live2",
    }, headers={"Cache-Control": "no-store"})


@app.post("/api/studio/stream-key")
async def api_studio_stream_key_save(request: Request):
    """Updates YT_STREAM_KEY in .env via write-env.sh and returns units needing restart."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "studio_key_update", max_calls=5, window_seconds=60)
    data = await request.json()
    new_key = str(data.get("stream_key", "")).strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="Stream key cannot be empty")
    try:
        units = await run_in_threadpool(env_store.write_updates, {"YT_STREAM_KEY": new_key})
    except (env_store.ValidationError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "studio_stream_key_update", f"ends in {new_key[-4:] if len(new_key)>=4 else 'key'}", deps.client_ip(request))
    return {
        "ok": True,
        "units_to_restart": units,
        "hint": f"••••••••••••{new_key[-4:]}" if len(new_key) >= 4 else "Configured",
    }


@app.get("/api/studio/settings")
async def api_studio_settings_get(request: Request):
    deps.require_session_api(request)
    return {
        "video_id": db.get_setting("yt_studio_video_id", "") or "",
        "watch_url": db.get_setting("yt_studio_watch_url", "") or "",
        "control_room_url": db.get_setting("yt_studio_control_room_url", "https://studio.youtube.com/channel/live/livestreaming") or "https://studio.youtube.com/channel/live/livestreaming",
    }


@app.post("/api/studio/settings")
async def api_studio_settings_save(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    data = await request.json()
    video_id = str(data.get("video_id", "")).strip()
    watch_url = str(data.get("watch_url", "")).strip()
    control_room_url = str(data.get("control_room_url", "")).strip()

    if video_id:
        yt_match = re.search(r"(?:v=|\/live\/|\/watch\?v=|\.be\/)([a-zA-Z0-9_-]{11})", video_id)
        if yt_match:
            video_id = yt_match.group(1)
        elif not re.match(r"^[a-zA-Z0-9_-]{8,15}$", video_id):
            raise HTTPException(status_code=400, detail="Invalid YouTube Video ID format")

    if watch_url and not (watch_url.startswith("https://") or watch_url.startswith("http://")):
        raise HTTPException(status_code=400, detail="Watch URL must start with http:// or https://")

    db.set_setting("yt_studio_video_id", video_id)
    if watch_url:
        db.set_setting("yt_studio_watch_url", watch_url)
    elif video_id:
        db.set_setting("yt_studio_watch_url", f"https://www.youtube.com/watch?v={video_id}")
    if control_room_url:
        db.set_setting("yt_studio_control_room_url", control_room_url)

    db.audit(session["username"], "studio_settings_update", video_id, deps.client_ip(request))
    return {
        "ok": True,
        "video_id": video_id,
        "watch_url": db.get_setting("yt_studio_watch_url", ""),
        "control_room_url": db.get_setting("yt_studio_control_room_url", "https://studio.youtube.com/channel/live/livestreaming"),
    }


# ---------------------------------------------------------------- api: text overlay studio (Part 2)

@app.get("/api/overlay")
async def api_overlay_get(request: Request):
    deps.require_session_api(request)
    from . import overlay
    return overlay.get_overlay_state()


def _apply_watermark_config(updated: dict) -> None:
    """Part 4: writes the real, encoder-consumed config (current-source.env's
    WATERMARK_* keys), and - the one genuinely new piece of state here -
    auto-switches a copy-mode direct source to re-encode, since a burned-in
    filter can't ride along with stream copy. Never silent - see the
    UI-facing one-line explanation this triggers in overlay.js."""
    active = sources_mod.get_active_source()
    if (
        updated.get("visible")
        and active
        and active["type"] == "direct"
        and (active.get("options") or {}).get("mode") == "copy"
    ):
        options = {**(active.get("options") or {}), "mode": "reencode"}
        sources_mod.update_source(
            active["id"], active["name"], active["type"], active["url"], options, active.get("account_id"),
        )
        active = sources_mod.get_active_source()

    control.write_watermark_config(updated)
    if active:
        control.write_current_source(active)


@app.post("/api/overlay")
async def api_overlay_save(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    data = await request.json()
    from . import overlay, control
    updated = overlay.save_overlay_state(data)
    await overlay.push_overlay_to_kiosk(updated)
    try:
        _apply_watermark_config(updated)
    except Exception as exc:
        logger.warning("failed to apply watermark config to the pipeline: %s", exc)
    db.audit(session["username"], "overlay_update", f"visible={updated.get('visible')}", deps.client_ip(request))
    return {
        "ok": True, "state": updated,
        "requires_restart": updated.get("visible", False) != control.watermark_is_running(),
    }


@app.post("/api/overlay/toggle")
async def api_overlay_toggle(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    from . import overlay, control
    updated = overlay.toggle_overlay_visibility()
    await overlay.push_overlay_to_kiosk(updated)
    try:
        _apply_watermark_config(updated)
    except Exception as exc:
        logger.warning("failed to apply watermark config to the pipeline: %s", exc)
    db.audit(session["username"], "overlay_toggle", f"visible={updated.get('visible')}", deps.client_ip(request))
    return {
        "ok": True, "state": updated,
        "requires_restart": updated.get("visible", False) != control.watermark_is_running(),
    }


@app.get("/api/overlay/status")
async def api_overlay_status(request: Request):
    deps.require_session_api(request)
    from . import overlay, control
    status = await overlay.get_overlay_kiosk_status()
    # The number that actually matters for Part 4: is the watermark
    # filter present in the *running* ffmpeg process right now, not just
    # saved to overlay.json. See control.watermark_is_running()'s
    # docstring for why the toggle can't just echo back the saved state.
    status["encoder_active"] = control.watermark_is_running()
    status["requires_restart"] = status.get("configured_visible", False) != status["encoder_active"]
    return status


@app.post("/api/overlay/image")
async def api_overlay_image_upload(request: Request):
    """Uploads a watermark image, stores it under zoombot (ffmpeg needs to
    read it, and this process runs as the dashboard user - same
    cross-user bridge as every other zoombot-owned write, see
    app/control.py's write_watermark_image())."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    from . import control, overlay

    form = await request.form()
    upload = form.get("image")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="no image file provided")
    ext = pathlib.Path(getattr(upload, "filename", "") or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(status_code=400, detail="image must be .png, .jpg, .jpeg, or .webp")
    content = await upload.read()
    MAX_BYTES = 5 * 1024 * 1024
    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="image must be 5MB or smaller")

    image_path = control.write_watermark_image(content, ext)
    updated = overlay.save_overlay_state({"image_path": image_path, "mode": "image"})
    db.audit(session["username"], "overlay_image_upload", f"path={image_path}", deps.client_ip(request))
    return {"ok": True, "image_path": image_path, "state": updated}


@app.post("/api/overlay/reinject")
async def api_overlay_reinject(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    from . import overlay
    res = await overlay.reinject_overlay()
    db.audit(
        session["username"],
        "overlay_reinject",
        f"connected={res.get('connected')}, injected={res.get('injected')}, visible={res.get('visible')}",
        deps.client_ip(request),
    )
    return {"ok": True, "status": res}


# ---------------------------------------------------------------- api: emergency failover BRB slate (Part 3)

@app.get("/api/slate/brb")
async def api_slate_brb_get(request: Request):
    deps.require_session_api(request)
    from . import slate
    return slate.get_brb_state()


@app.post("/api/slate/brb")
async def api_slate_brb_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    data = await request.json()
    action = str(data.get("action", "toggle")).lower()
    from . import slate
    if action == "show":
        state = slate.set_brb_state(True, title=data.get("title"), subtitle=data.get("subtitle"))
    elif action == "hide":
        state = slate.set_brb_state(False)
    elif action == "toggle":
        state = slate.toggle_brb_state()
    else:
        raise HTTPException(status_code=400, detail="action must be show, hide, or toggle")
    await slate.push_brb_to_kiosk(state)
    db.audit(session["username"], "slate_brb", f"active={state.get('active')}", deps.client_ip(request))
    return {"ok": True, "state": state}


# ---------------------------------------------------------------- api: local stream recording (Part 5)

@app.get("/api/record")
async def api_record_get(request: Request):
    deps.require_session_api(request)
    try:
        return await run_in_threadpool(recording.get_status)
    except Exception:
        return {"recording": False, "file": "", "duration": 0, "size_mb": 0.0, "free_gb": recording.get_free_disk_gb(), "halted_reason": ""}


@app.post("/api/record")
async def api_record_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    data = await request.json()
    action = str(data.get("action", "")).lower()
    if action not in ("start", "stop", "status"):
        raise HTTPException(status_code=400, detail="action must be start, stop or status")
    try:
        if action == "start":
            result = await run_in_threadpool(recording.record_start)
        elif action == "stop":
            result = await run_in_threadpool(recording.record_stop)
        else:
            result = await run_in_threadpool(recording.get_status)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], f"record_stream_{action}", "", deps.client_ip(request))
    return result


@app.post("/api/recordings/clean")
@app.get("/api/recordings/clean")
async def api_recordings_clean(request: Request):
    session = deps.require_session_api(request)
    if request.method == "POST":
        deps.require_csrf(request, session)
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        body = dict(request.query_params)
    force = bool(body.get("force", False) or body.get("all", False))
    try:
        max_age = float(body.get("max_age_hours", 24.0))
    except (ValueError, TypeError):
        max_age = 24.0
    result = await run_in_threadpool(recording.clean_recordings, max_age_hours=max_age, force=force)
    db.audit(session["username"], "recordings_clean", f"deleted={result.get('deleted_count')} freed_mb={result.get('freed_mb')}", deps.client_ip(request))
    return result


@app.get("/api/settings/telegram")
async def api_settings_telegram_get(request: Request):
    deps.require_session_api(request)
    return settings_store.masked_settings()


@app.post("/api/settings/telegram")
async def api_settings_telegram_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    updates = await request.json()
    settings_store.save_settings(updates)
    db.audit(session["username"], "telegram_settings_update", ip=deps.client_ip(request))
    return {"ok": True, "settings": settings_store.masked_settings()}


@app.post("/api/telegram/test")
async def api_telegram_test_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    try:
        data = await request.json()
    except Exception:
        data = {}
    res = await telegram.test_telegram_connection(
        bot_token=str(data.get("telegram_bot_token", "")),
        chat_id=str(data.get("telegram_chat_id", "")),
        api_id=str(data.get("telegram_api_id", "")),
        api_hash=str(data.get("telegram_api_hash", "")),
        session_string=str(data.get("telegram_session_string", "")),
    )
    db.audit(session["username"], "telegram_test", f"ok={res.get('ok')}", deps.client_ip(request))
    return res


@app.post("/api/settings")
async def api_settings_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    if "auto_recovery" in body:
        db.set_setting("auto_recovery", "on" if body["auto_recovery"] else "off")
    if "webhook_url" in body:
        db.set_setting("webhook_url", str(body["webhook_url"])[:500])
    db.audit(session["username"], "settings_update", ip=deps.client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------- api: profiles

@app.get("/api/profiles")
async def api_profiles_list(request: Request):
    deps.require_session_api(request)
    return profiles_mod.list_profiles()


@app.post("/api/profiles")
async def api_profiles_create(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        pid = profiles_mod.create_profile(
            body.get("name", ""), body.get("zoom_link", ""),
            body.get("zoom_passcode", ""), body.get("bot_name", "Stream Bot"),
        )
    except env_store.ValidationError as exc:
        return _api_error(exc)
    db.audit(session["username"], "profile_create", body.get("name", ""), deps.client_ip(request))
    return {"id": pid}


@app.put("/api/profiles/{profile_id}")
async def api_profiles_update(profile_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        profiles_mod.update_profile(
            profile_id, body.get("name", ""), body.get("zoom_link", ""),
            body.get("zoom_passcode", ""), body.get("bot_name", "Stream Bot"),
        )
    except env_store.ValidationError as exc:
        return _api_error(exc)
    db.audit(session["username"], "profile_update", str(profile_id), deps.client_ip(request))
    return {"ok": True}


@app.delete("/api/profiles/{profile_id}")
async def api_profiles_delete(profile_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    profiles_mod.delete_profile(profile_id)
    db.audit(session["username"], "profile_delete", str(profile_id), deps.client_ip(request))
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/switch")
async def api_profiles_switch(profile_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    p = profiles_mod.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="profile not found")
    units = await run_in_threadpool(env_store.write_updates, {
        "ZOOM_LINK": p["zoom_link"], "ZOOM_PASSCODE": p["zoom_passcode"] or "", "BOT_NAME": p["bot_name"],
    })
    db.audit(session["username"], "profile_switch", p["name"], deps.client_ip(request))
    return {"units_to_restart": units}


# ---------------------------------------------------------------- api: sources (Change 1)

@app.get("/api/sources")
async def api_sources_list(request: Request):
    deps.require_session_api(request)
    active = sources_mod.get_active_source()
    rev = sources_mod.sources_rev()
    # Cheap poll for the live-sync pages: ?since=<rev> -> {"changed": false}
    since = request.query_params.get("since")
    if since is not None and since.isdigit() and int(since) == rev:
        return {"changed": False, "rev": rev}
    return {"changed": True, "rev": rev, "sources": sources_mod.list_sources_public(),
            "active_id": active["id"] if active else None}


@app.get("/api/sources/{source_id}/reveal")
async def api_sources_reveal(source_id: int, request: Request):
    """The masked secrets of one Zoom source (full link, passcode,
    personal join link) for an explicit reveal tap. Rate-limited and
    audited; never cached by the page."""
    session = deps.require_session_api(request)
    deps.require_rate_limit(session, "source_reveal", max_calls=10, window_seconds=60)
    try:
        secrets = sources_mod.reveal_secrets(source_id)
    except env_store.ValidationError as exc:
        return _api_error(exc)
    db.audit(session["username"], "source_reveal", str(source_id), deps.client_ip(request))
    return JSONResponse(secrets, headers={"Cache-Control": "no-store"})


@app.post("/api/sources/{source_id}/duplicate")
async def api_sources_duplicate(source_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    try:
        sid = sources_mod.duplicate_source(source_id)
    except env_store.ValidationError as exc:
        return _api_error(exc)
    db.audit(session["username"], "source_duplicate", f"{source_id}->{sid}", deps.client_ip(request))
    return {"id": sid, "source": sources_mod.public_view(sources_mod.get_source(sid))}


@app.post("/api/sources/detect-type")
async def api_sources_detect_type(request: Request):
    deps.require_session_api(request)
    body = await request.json()
    return {"type": sources_mod.detect_type(str(body.get("url", "")))}


@app.post("/api/sources/probe")
async def api_sources_probe(request: Request):
    deps.require_session_api(request)
    body = await request.json()
    url = str(body.get("url", ""))
    try:
        validated = await run_in_threadpool(url_security.validate_url, url, "direct")
    except URLSecurityError as exc:
        return _api_error(exc)
    try:
        result = await probe_mod.probe_url(validated)
    except probe_mod.ProbeError as exc:
        return _api_error(exc)
    return result


@app.post("/api/sources")
async def api_sources_create(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        sid = sources_mod.create_source(
            body.get("name", ""), body.get("type", ""),
            body.get("url", ""), body.get("options", {}), body.get("account_id"),
        )
    except (env_store.ValidationError, URLSecurityError) as exc:
        return _api_error(exc)
    scheduler.load_schedules()
    db.audit(session["username"], "source_create", str(body.get("name", ""))[:64], deps.client_ip(request))
    return {"id": sid, "source": sources_mod.public_view(sources_mod.get_source(sid))}


@app.put("/api/sources/{source_id}")
async def api_sources_update(source_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        sources_mod.update_source(
            source_id, body.get("name", ""), body.get("type", ""),
            body.get("url", ""), body.get("options", {}), body.get("account_id"),
        )
    except (env_store.ValidationError, URLSecurityError) as exc:
        return _api_error(exc)
    scheduler.load_schedules()
    db.audit(session["username"], "source_update", str(source_id), deps.client_ip(request))
    return {"ok": True, "source": sources_mod.public_view(sources_mod.get_source(source_id))}


@app.post("/api/sources/{source_id}/account")
async def api_sources_set_account(source_id: int, request: Request):
    """Bind/unbind the Google account (Chrome profile) this source plays
    or joins as. Takes effect on the next switch to the source."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    s = sources_mod.get_source(source_id)
    if not s:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        sources_mod.set_account(source_id, body.get("account_id"))
    except env_store.ValidationError as exc:
        return _api_error(exc)
    applied = False
    # apply_now: the source is the active web source, so relaunch just the
    # browser on the new profile. Chrome cannot change user-data-dir in
    # place; the encoder keeps running (RTMP stays up), viewers see the
    # page reload. Never automatic while the stream is live - the UI asks.
    if body.get("apply_now"):
        active = sources_mod.get_active_source()
        if active and active["id"] == source_id and s["type"] == "webpage":
            try:
                fresh = sources_mod.get_source(source_id)
                await run_in_threadpool(control.apply_source_config, fresh)
                await run_in_threadpool(control.unit_action, "browser-source", "restart")
                applied = True
            except control.ControlError as exc:
                return _api_error(exc)
    db.audit(session["username"], "source_bind_account", f"{source_id}:{body.get('account_id')} applied={applied}", deps.client_ip(request))
    return {"ok": True, "applied": applied}


@app.post("/api/zoom/classify")
async def api_zoom_classify(request: Request):
    deps.require_session_api(request)
    body = await request.json()
    from . import zoomlink
    return zoomlink.classify(str(body.get("url", "")))


@app.post("/api/sources/{source_id}/registration/open")
async def api_sources_registration_open(source_id: int, request: Request):
    """Opens the source's Zoom registration page in an ordinary Chrome
    window on :99 (bound account's profile, or the 'default' one) for
    the admin to complete over noVNC. Nothing is filled in for them."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "registration_open", max_calls=4, window_seconds=60)
    s = sources_mod.get_source(source_id)
    if not s or s["type"] != "zoom":
        raise HTTPException(status_code=404, detail="zoom source not found")
    from . import zoomlink
    try:
        zoomlink.validate_registration(s["url"])
        url = await run_in_threadpool(url_security.validate_url, s["url"], "webpage")
    except (zoomlink.ZoomLinkError, URLSecurityError) as exc:
        return _api_error(exc)
    profile_id = control._account_profile_id(s) or "default"
    try:
        if profile_id == "default":
            await run_in_threadpool(control.account_profile_action, "create", "default")
        await run_in_threadpool(control.open_url_in_account_profile, profile_id, url)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_registration_open", str(source_id), deps.client_ip(request))
    return {"ok": True, "profile_id": profile_id}


@app.post("/api/sources/{source_id}/join-url")
async def api_sources_join_url(source_id: int, request: Request):
    """Save the personal (tk=) join link Zoom issued after registering."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        s = sources_mod.set_zoom_join_url(source_id, str(body.get("join_url", "")))
        # Close the registration window if it's still open; the profile
        # used was whichever registration/open picked.
        profile_id = control._account_profile_id(s) or "default"
        await run_in_threadpool(control.account_profile_action, "close", profile_id)
    except env_store.ValidationError as exc:
        return _api_error(exc)
    except control.ControlError:
        pass
    db.audit(session["username"], "zoom_join_url_saved", str(source_id), deps.client_ip(request))
    return {"ok": True, "link_kind": s["link_kind"], "join_ready": s["join_ready"], "source": sources_mod.public_view(s)}


@app.delete("/api/sources/{source_id}")
async def api_sources_delete(source_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    sources_mod.delete_source(source_id)
    scheduler.load_schedules()
    db.audit(session["username"], "source_delete", str(source_id), deps.client_ip(request))
    return {"ok": True}


@app.post("/api/sources/{source_id}/switch")
async def api_sources_switch(source_id: int, request: Request):
    """Keeps RTMP up whenever possible - see control.start_source() for
    exactly when it can and can't. Response carries rtmp_dropped /
    hot_swapped so the UI can say what actually happened."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "source_switch", max_calls=6, window_seconds=30)
    s = sources_mod.get_source(source_id)
    if not s:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        outcome = await run_in_threadpool(control.start_source, s)
    except control.ControlError as exc:
        return _api_error(exc)
    sources_mod.set_active_source_id(source_id)
    if s["type"] == "zoom":
        sources_mod.mark_joined(source_id)
        join_via = "web" if control.producer_unit_for(s) == "browser-source" else "client"
        _schedule_zoom_followup(s, join_via)
    db.audit(session["username"], "source_switch",
             f"{s['name']} hot={outcome['hot_swapped']} dropped={outcome['rtmp_dropped']}", deps.client_ip(request))
    return outcome


@app.get("/api/sources/{source_id}/switch-preview")
async def api_sources_switch_preview(source_id: int, request: Request):
    """What a switch to this source would do to the stream, so the UI can
    confirm honestly ("will restart the encoder") before doing it."""
    deps.require_session_api(request)
    s = sources_mod.get_source(source_id)
    if not s:
        raise HTTPException(status_code=404, detail="source not found")
    old_type = control.read_current_source().get("SOURCE_TYPE", "")
    ffmpeg_up = await run_in_threadpool(control._ffmpeg_is_up)
    needs_restart = s["type"] == "direct" or old_type == "direct"
    return {"ffmpeg_up": ffmpeg_up, "rtmp_would_drop": needs_restart and ffmpeg_up,
            "join_ready": s.get("join_ready", True), "missing": s.get("missing", [])}


# ---------------------------------------------------------------- api: Zoom Google sign-in (Change 2)

@app.get("/api/zoom/account")
async def api_zoom_account(request: Request):
    deps.require_session_api(request)
    heuristic = await run_in_threadpool(control.zoom_session_heuristic)
    return {
        "label": db.get_setting("zoom_account_label", ""),
        "confirmed_signed_in": db.get_setting("zoom_account_signed_in", "0") == "1",
        "heuristic": heuristic,
    }


@app.post("/api/zoom/account")
async def api_zoom_account_set(request: Request):
    """The admin's own confirmation after completing sign-in/out by hand
    over noVNC - see control.py's module note on why this, not scraped
    account info, is the authoritative record."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    signed_in = bool(body.get("signed_in"))
    label = str(body.get("label", ""))[:120]
    db.set_setting("zoom_account_signed_in", "1" if signed_in else "0")
    db.set_setting("zoom_account_label", label if signed_in else "")
    db.audit(session["username"], "zoom_account_update", f"signed_in={signed_in}", deps.client_ip(request))
    return {"ok": True}


@app.post("/api/zoom/google-signin")
async def api_zoom_google_signin(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    try:
        result = await run_in_threadpool(control.zoom_google_signin)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_google_signin", ip=deps.client_ip(request))
    return result


@app.post("/api/zoom/signout")
async def api_zoom_signout(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    try:
        result = await run_in_threadpool(control.zoom_signout)
    except control.ControlError as exc:
        return _api_error(exc)
    db.set_setting("zoom_account_signed_in", "0")
    db.set_setting("zoom_account_label", "")
    db.audit(session["username"], "zoom_signout", ip=deps.client_ip(request))
    return result


# ---------------------------------------------------------------- api: accounts (Part 2)
#
# No route here ever receives or returns a password, 2FA code, token or
# cookie. Sign-in happens in a Chrome window over noVNC; these routes
# only open/close that window and ask Google whether a session exists.

@app.get("/api/accounts")
async def api_accounts_list(request: Request):
    deps.require_session_api(request)
    return accounts_mod.list_accounts()


@app.post("/api/accounts")
async def api_accounts_create(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_create", max_calls=5, window_seconds=60)
    body = await request.json()
    try:
        aid = await run_in_threadpool(accounts_mod.create_account, str(body.get("label", "")))
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_create", str(aid), deps.client_ip(request))
    return {"id": aid}


@app.post("/api/accounts/import")
async def api_accounts_import(request: Request):
    """New account from the shared stream profile's existing session (the
    one Zoom's Google sign-in / the kiosk used). Nothing is read from the
    profile here - it is copied as zoombot and then verified with Google."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_import", max_calls=3, window_seconds=60)
    body = await request.json()
    try:
        aid = await run_in_threadpool(accounts_mod.import_stream_profile, str(body.get("label", "")))
        async with _verify_lock:
            result = await run_in_threadpool(accounts_mod.verify_account, aid)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_import", f"{aid}:{result['state']}", deps.client_ip(request))
    return {"id": aid, "verify": result}


@app.post("/api/accounts/{account_id}/signout")
async def api_accounts_signout(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    try:
        await run_in_threadpool(accounts_mod.sign_out, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_signout", str(account_id), deps.client_ip(request))
    return {"ok": True}


@app.post("/api/accounts/{account_id}/backup")
async def api_accounts_backup(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_backup", max_calls=4, window_seconds=60)
    try:
        result = await run_in_threadpool(accounts_mod.backup_account, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_backup", f"{account_id}:{result['backup']}", deps.client_ip(request))
    return result


@app.get("/api/accounts/{account_id}/backups")
async def api_accounts_backups(account_id: int, request: Request):
    deps.require_session_api(request)
    try:
        return await run_in_threadpool(accounts_mod.list_backups, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)


@app.post("/api/accounts/{account_id}/restore")
async def api_accounts_restore(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_restore", max_calls=4, window_seconds=60)
    body = await request.json()
    name = str(body.get("name") or "") or None
    try:
        async with _verify_lock:
            result = await run_in_threadpool(accounts_mod.restore_account, account_id, name)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_restore", f"{account_id}:{result.get('restored', '')}:{result['state']}", deps.client_ip(request))
    return result


@app.put("/api/accounts/{account_id}")
async def api_accounts_rename(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        accounts_mod.rename_account(account_id, str(body.get("label", "")))
    except AccountError as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_rename", str(account_id), deps.client_ip(request))
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def api_accounts_delete(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_delete", max_calls=5, window_seconds=60)
    try:
        await run_in_threadpool(accounts_mod.remove_account, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_delete", str(account_id), deps.client_ip(request))
    return {"ok": True}


@app.post("/api/accounts/{account_id}/signin/start")
async def api_accounts_signin_start(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_signin", max_calls=6, window_seconds=60)
    try:
        result = await run_in_threadpool(accounts_mod.signin_start, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_signin_start", str(account_id), deps.client_ip(request))
    return result


@app.post("/api/accounts/{account_id}/signin/cancel")
async def api_accounts_signin_cancel(account_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    try:
        await run_in_threadpool(accounts_mod.signin_cancel, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "account_signin_cancel", str(account_id), deps.client_ip(request))
    return {"ok": True}


@app.get("/api/accounts/{account_id}/signin/status")
async def api_accounts_signin_status(account_id: int, request: Request):
    deps.require_session_api(request)
    try:
        return await run_in_threadpool(accounts_mod.signin_status, account_id)
    except (AccountError, control.ControlError) as exc:
        return _api_error(exc)


_verify_lock = asyncio.Lock()  # one headless verify at a time (fixed profile lock semantics)


@app.post("/api/accounts/{account_id}/verify")
async def api_accounts_verify(account_id: int, request: Request):
    """Also the "I'm done" step of sign-in: closes the sign-in window
    (flushing cookies) and asks Google whether a session now exists."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "account_verify", max_calls=6, window_seconds=60)
    async with _verify_lock:
        try:
            result = await run_in_threadpool(accounts_mod.verify_account, account_id)
        except (AccountError, control.ControlError) as exc:
            return _api_error(exc)
    db.audit(session["username"], "account_verify", f"{account_id}:{result['state']}", deps.client_ip(request))
    return result


# ---------------------------------------------------------------- api: touch remote / media control (Part 3)

@app.get("/api/audio/stream")
async def api_audio_stream_get(request: Request):
    deps.require_session_api(request)
    try:
        result = await run_in_threadpool(control.stream_audio_action, "status")
    except control.ControlError as exc:
        return _api_error(exc)
    return result


@app.post("/api/audio/stream")
async def api_audio_stream_post(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "audio_stream", max_calls=10, window_seconds=10)
    body = await request.json()
    action = str(body.get("action", ""))
    if action not in ("mute", "unmute", "volume"):
        raise HTTPException(status_code=400, detail="action must be mute, unmute or volume")
    try:
        volume = int(body.get("volume", 100)) if action == "volume" else None
        result = await run_in_threadpool(control.stream_audio_action, action, volume)
    except (control.ControlError, ValueError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "audio_stream_" + action, str(volume) if volume is not None else "", deps.client_ip(request))
    return result


@app.get("/api/audio/level")
async def api_audio_level(request: Request):
    """Live level of what viewers hear (zoom_out.monitor). Polling this
    is what keeps the sampler running - it stops by itself ~15s after the
    last poll, so it costs nothing when no panel is open."""
    deps.require_session_api(request)
    await audio_level.manager.touch()
    return audio_level.manager.snapshot()


@app.get("/api/audio/zoom-mic")
async def api_zoom_mic_state(request: Request):
    deps.require_session_api(request)
    return await run_in_threadpool(control.zoom_mic_state)


@app.post("/api/audio/zoom-mic/toggle")
async def api_zoom_mic_toggle(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_mic_toggle", max_calls=6, window_seconds=10)
    try:
        result = await run_in_threadpool(control.zoom_mic_toggle)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_mic_toggle", ip=deps.client_ip(request))
    return result


@app.post("/api/zoom/leave-with-choice")
async def api_zoom_leave_with_choice(request: Request):
    """Distinct from POST /api/zoom/leave (used by the desktop Controls
    page, which just stops zoom.service): this is the touch remote's
    "Leave meeting" action, which also decides what happens to the
    stream afterwards."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_leave", max_calls=3, window_seconds=30)
    body = await request.json()
    then = str(body.get("then", "slate"))
    if then not in ("stop", "slate"):
        raise HTTPException(status_code=400, detail="'then' must be 'stop' or 'slate'")
    try:
        results = await run_in_threadpool(control.zoom_leave, then)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_leave", then, deps.client_ip(request))
    return {"results": results}


@app.get("/api/youtube-links")
async def api_youtube_links_list(request: Request):
    deps.require_session_api(request)
    return youtube_watch.list_links()


@app.post("/api/youtube/parse")
async def api_youtube_parse(request: Request):
    """Smart paste: any YouTube link form (or share text / bare id) ->
    what it is, normalized, plus title/channel/thumbnail from YouTube's
    public oEmbed, for the operator to confirm before saving."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "youtube_parse", max_calls=20, window_seconds=30)
    body = await request.json()
    parsed = youtube.parse_any(str(body.get("text", ""))[:20000])
    if parsed.get("ok"):
        try:
            await run_in_threadpool(url_security.validate_url, parsed["url"], "webpage")
        except URLSecurityError as exc:
            parsed["ok"] = False; parsed["errors"].append(str(exc))
            return parsed
        meta = await run_in_threadpool(youtube.lookup, parsed["url"])
        if meta.get("unavailable"):
            parsed["warnings"].append("YouTube has no public details for this (private, removed, or members-only?) - it may not play.")
        else:
            parsed.update({k: v for k, v in meta.items() if v})
        if parsed["kind"] == "live_channel":
            try:
                r = await run_in_threadpool(youtube.resolve_live, parsed["url"])
                if r["video_id"]:
                    parsed["live_now"] = True; parsed["thumbnail_url"] = f"https://i.ytimg.com/vi/{r['video_id']}/hqdefault.jpg"
                    live_meta = await run_in_threadpool(youtube.lookup, f"https://www.youtube.com/watch?v={r['video_id']}")
                    parsed.update({k: v for k, v in live_meta.items() if v})
                else:
                    parsed["live_now"] = False; parsed["warnings"].append("This channel is not live right now.")
            except youtube.YouTubeURLError as exc:
                parsed["warnings"].append(str(exc))
    return parsed


@app.get("/api/youtube/settings")
async def api_youtube_settings(request: Request):
    deps.require_session_api(request)
    return youtube_watch.get_settings()


@app.put("/api/youtube/settings")
async def api_youtube_settings_set(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    result = youtube_watch.set_settings({k: bool(body.get(k)) for k in youtube_watch.SETTING_KEYS if k in body})
    db.audit(session["username"], "youtube_settings", ",".join(f"{k}={int(v)}" for k, v in result.items()), deps.client_ip(request))
    return result


def _link_row(link_id: int) -> dict | None:
    for l in youtube_watch.list_links():
        if l["id"] == link_id:
            return l
    return None


async def play_youtube_link(link: dict | None = None, url: str | None = None, options: dict | None = None) -> str:
    """Navigate the kiosk to a saved link (with its options) or a raw
    URL. The single path every play goes through - taps, next/prev,
    auto-advance. Returns the embed URL."""
    raw_url = link["url"] if link else (url or "")
    opts = youtube.validate_options(link["options"] if link else (options or {}))
    embed_url = await run_in_threadpool(youtube.to_play_url, raw_url, opts)
    await cdp.navigate(embed_url)
    youtube_watch.current.update({"link_id": link["id"] if link else None, "url": raw_url, "since": time.time()})
    if link:
        with db.get_conn() as conn:
            conn.execute("UPDATE youtube_links SET last_played_at=?, plays=plays+1 WHERE id=?", (time.time(), link["id"]))
    # The watch page needs a moment before a <video> exists - poll rather
    # than blind-sleep, then apply what the URL can't carry (loop,
    # captions, speed) and fullscreen the player so it fills the canvas
    # like the old embed did (the kiosk window is already fullscreen).
    for _ in range(16):
        await asyncio.sleep(0.5)
        try:
            res = await cdp.evaluate(cdp.JS_ENSURE_UNMUTED)
        except cdp.CDPError:
            continue
        if res.get("value"):
            try:
                await cdp.evaluate(cdp.js_apply_options(bool(opts.get("loop")), bool(opts.get("captions")), float(opts.get("speed") or 1.0)))
                await cdp.evaluate(cdp.JS_PLAY, user_gesture=True)
            except cdp.CDPError:
                pass
            # Fullscreen the player once the page reports it isn't; the
            # button exists before it works, so retry a few times.
            if youtube_watch.get_settings().get("yt_keep_fullscreen", True):
                for _ in range(6):
                    try:
                        st = (await cdp.evaluate(cdp.JS_STATE)).get("value") or {}
                        if st.get("fullscreen"):
                            break
                        await cdp.evaluate(cdp.JS_FULLSCREEN, user_gesture=True)
                    except cdp.CDPError:
                        break
                    await asyncio.sleep(1.0)
            break
    return embed_url


@app.get("/api/youtube/state")
async def api_youtube_state(request: Request):
    """Live player state read off the kiosk tab (never cached), plus the
    spec's playback diagnosis (sign-in / age-restricted / error) with
    the concrete fix for each."""
    deps.require_session_api(request)
    try:
        res = await cdp.evaluate(cdp.JS_STATE)
    except cdp.CDPError as exc:
        return {"available": False, "reason": str(exc)}
    state = res.get("value") or {}
    state["available"] = True
    state["diagnosis"] = cdp.diagnose(state)
    state.update(youtube_watch.phase(state))
    state["current_link_id"] = youtube_watch.current["link_id"]
    state["auto"] = youtube_watch.last.get("auto", [])[:3]
    state["settings"] = youtube_watch.get_settings()
    return state


@app.post("/api/youtube-links")
async def api_youtube_links_create(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        row = await _youtube_link_from_body(body)
    except (YouTubeURLError, URLSecurityError) as exc:
        return _api_error(exc)
    with db.get_conn() as conn:
        order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM youtube_links").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO youtube_links (name, url, created_at, sort_order, kind, title, author, thumbnail_url, options) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["name"], row["url"], time.time(), order, row["kind"], row["title"], row["author"], row["thumbnail_url"], json.dumps(row["options"])),
        )
        lid = cur.lastrowid
    db.audit(session["username"], "youtube_link_create", row["name"], deps.client_ip(request))
    return {"id": lid, "link": _link_row(lid)}


async def _youtube_link_from_body(body: dict, existing: dict | None = None) -> dict:
    """Validate a create/edit payload: the URL is parsed and normalized,
    options validated, metadata looked up when the URL changed."""
    url = str(body.get("url", "") or "").strip() or (existing["url"] if existing else "")
    parsed = youtube.parse_any(url)
    if not parsed["ok"]:
        raise YouTubeURLError(parsed["errors"][0] if parsed["errors"] else "Not a playable YouTube link")
    url = parsed["url"]
    await run_in_threadpool(url_security.validate_url, url, "webpage")
    options = youtube.validate_options(body.get("options") if body.get("options") is not None else (existing["options"] if existing else {}))
    if options["start"] is None and parsed.get("start"):
        options["start"] = parsed["start"]
    name = str(body.get("name", "") or "").strip()[:120]
    meta = {}
    if not existing or existing["url"] != url:
        meta = await run_in_threadpool(youtube.lookup, url)
    title = meta.get("title") or (existing["title"] if existing else None)
    author = meta.get("author") or (existing["author"] if existing else None)
    thumb = meta.get("thumbnail_url") or parsed.get("thumbnail_url") or (existing["thumbnail_url"] if existing else None)
    if not name:
        name = (title or (existing["name"] if existing else "") or {"playlist": "Playlist", "live_channel": (parsed.get("channel") or "Channel") + " live"}.get(parsed["kind"], "Video " + (parsed.get("video_id") or "")))[:120]
    return {"name": name, "url": url, "kind": parsed["kind"], "title": title, "author": author, "thumbnail_url": thumb, "options": options}


@app.put("/api/youtube-links/{link_id}")
async def api_youtube_links_update(link_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    existing = _link_row(link_id)
    if not existing:
        raise HTTPException(status_code=404, detail="saved link not found")
    body = await request.json()
    try:
        row = await _youtube_link_from_body(body, existing)
    except (YouTubeURLError, URLSecurityError) as exc:
        return _api_error(exc)
    with db.get_conn() as conn:
        conn.execute("UPDATE youtube_links SET name=?, url=?, kind=?, title=?, author=?, thumbnail_url=?, options=? WHERE id=?",
                     (row["name"], row["url"], row["kind"], row["title"], row["author"], row["thumbnail_url"], json.dumps(row["options"]), link_id))
    db.audit(session["username"], "youtube_link_update", str(link_id), deps.client_ip(request))
    return {"ok": True, "link": _link_row(link_id)}


@app.post("/api/youtube-links/{link_id}/duplicate")
async def api_youtube_links_duplicate(link_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    l = _link_row(link_id)
    if not l:
        raise HTTPException(status_code=404, detail="saved link not found")
    with db.get_conn() as conn:
        order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM youtube_links").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO youtube_links (name, url, created_at, sort_order, kind, title, author, thumbnail_url, options) VALUES (?,?,?,?,?,?,?,?,?)",
            ((l["name"] + " (copy)")[:120], l["url"], time.time(), order, l["kind"], l["title"], l["author"], l["thumbnail_url"], json.dumps(l["options"])),
        )
        lid = cur.lastrowid
    db.audit(session["username"], "youtube_link_duplicate", f"{link_id}->{lid}", deps.client_ip(request))
    return {"id": lid, "link": _link_row(lid)}


@app.post("/api/youtube-links/reorder")
async def api_youtube_links_reorder(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    with db.get_conn() as conn:
        for i, lid in enumerate(ids):
            conn.execute("UPDATE youtube_links SET sort_order=? WHERE id=?", (i + 1, lid))
    db.audit(session["username"], "youtube_links_reorder", str(len(ids)), deps.client_ip(request))
    return {"ok": True}


@app.delete("/api/youtube-links/{link_id}")
async def api_youtube_links_delete(link_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM youtube_links WHERE id=?", (link_id,))
    db.audit(session["username"], "youtube_link_delete", str(link_id), deps.client_ip(request))
    return {"ok": True}


@app.post("/api/youtube/play")
async def api_youtube_play(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "youtube_play", max_calls=6, window_seconds=15)
    body = await request.json()
    raw_url = str(body.get("url", "")).strip()
    link_id = body.get("link_id")
    if link_id and not raw_url:
        with db.get_conn() as conn:
            row = conn.execute("SELECT url FROM youtube_links WHERE id=?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="saved link not found")
        raw_url = row["url"]
    if not raw_url:
        raise HTTPException(status_code=400, detail="url or link_id is required")
    link = _link_row(int(link_id)) if link_id and not str(body.get("url", "")).strip() else None
    try:
        if link:
            embed_url = await play_youtube_link(link)
        else:
            parsed = youtube.parse_any(raw_url)
            if not parsed["ok"]:
                raise YouTubeURLError(parsed["errors"][0] if parsed["errors"] else "Not a playable YouTube link")
            embed_url = await play_youtube_link(url=parsed["url"], options=body.get("options") or {})
    except (YouTubeURLError, URLSecurityError, cdp.CDPError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "youtube_play", raw_url[:200], deps.client_ip(request))
    return {"ok": True, "embed_url": embed_url, "current_link_id": youtube_watch.current["link_id"]}


@app.post("/api/youtube/control")
async def api_youtube_control(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "youtube_control", max_calls=15, window_seconds=10)
    body = await request.json()
    action = str(body.get("action", ""))
    try:
        if action == "play":
            await cdp.evaluate(cdp.JS_PLAY, user_gesture=True)
        elif action == "pause":
            await cdp.evaluate(cdp.JS_PAUSE)
        elif action == "volume":
            level = int(body.get("level", 100))
            await cdp.evaluate(cdp.js_set_volume(level))
        elif action == "mute":
            await cdp.evaluate(cdp.JS_MUTE)
        elif action == "unmute":
            await cdp.evaluate(cdp.JS_UNMUTE)
        elif action == "seek":
            await cdp.evaluate(cdp.js_seek(float(body.get("seconds", 0))))
        elif action == "theater":
            await cdp.evaluate(cdp.JS_THEATER)
        elif action == "fullscreen":
            await cdp.evaluate(cdp.JS_FULLSCREEN, user_gesture=True)
        elif action == "speed":
            rate = float(body.get("rate", 1.0))
            if rate not in youtube.SPEEDS:
                raise HTTPException(status_code=400, detail="invalid speed")
            res = await cdp.evaluate(cdp.js_set_speed(rate))
            return {"ok": True, "speed": res.get("value")}
        elif action == "captions":
            res = await cdp.evaluate(cdp.JS_TOGGLE_CAPTIONS, user_gesture=True)
            return {"ok": True, "captions": res.get("value")}
        elif action == "skip-ad":
            res = await cdp.evaluate(cdp.JS_SKIP_AD, user_gesture=True)
            return {"ok": True, "skipped": bool(res.get("value"))}
        elif action == "dismiss-prompt":
            res = await cdp.evaluate(cdp.JS_DISMISS_PROMPT, user_gesture=True)
            return {"ok": True, "dismissed": bool(res.get("value"))}
        elif action == "replay":
            await cdp.evaluate(cdp.JS_REPLAY, user_gesture=True)
        elif action in ("next", "prev"):
            nxt = youtube_watch.neighbour(1 if action == "next" else -1)
            if not nxt:
                raise HTTPException(status_code=400, detail="no saved links")
            await play_youtube_link(nxt)
            return {"ok": True, "current_link_id": nxt["id"], "name": nxt["name"]}
        elif action == "retry":
            cur = youtube_watch.current
            if cur.get("link_id") and _link_row(cur["link_id"]):
                await play_youtube_link(_link_row(cur["link_id"]))
            elif cur.get("url"):
                await play_youtube_link(url=cur["url"])
            else:
                raise HTTPException(status_code=400, detail="nothing to retry")
        else:
            raise HTTPException(status_code=400, detail="invalid action")
    except (cdp.CDPError, ValueError, YouTubeURLError, URLSecurityError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "youtube_control_" + action, ip=deps.client_ip(request))
    return {"ok": True}


@app.get("/api/zoom/status")
async def api_zoom_status(request: Request):
    """Meeting status + mic/camera readback. Every field is a best-effort
    heuristic (authoritative=False) - see control.zoom_meeting_status."""
    deps.require_session_api(request)
    return await run_in_threadpool(control.zoom_meeting_status)


@app.post("/api/zoom/control")
async def api_zoom_control(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_control", max_calls=10, window_seconds=10)
    body = await request.json()
    action = str(body.get("action", ""))
    if action not in control.ZOOM_SHORTCUT_ACTIONS:
        raise HTTPException(status_code=400, detail="invalid action")
    try:
        result = await run_in_threadpool(control.zoom_shortcut, action)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_control_" + action, ip=deps.client_ip(request))
    return result


@app.get("/api/browser/status")
async def api_browser_status(request: Request):
    """Is the kiosk Chrome controllable right now? connected=True with
    the Chrome version and current page, or connected=False with a
    concrete reason code + what to do about it (see
    control.browser_diagnosis). Polled by the panel while a web source
    is active."""
    deps.require_session_api(request)
    try:
        return await cdp.probe()
    except cdp.CDPError as exc:
        diag = await run_in_threadpool(control.browser_diagnosis)
        diag["detail"] = str(exc)
        return diag


@app.post("/api/browser/open")
async def api_browser_open(request: Request):
    """"Browser" on the touch remote: put Google (or another https page)
    on :99 in the profile that already holds the operator's Google
    session, so it comes up signed in. Prefers navigating the kiosk tab
    over DevTools (one window, YouTube controls keep working); when no
    controllable kiosk exists, open-browser.sh opens a window in that
    same profile. Reports which account the page shows, masked."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "browser_open", max_calls=6, window_seconds=20)
    body = await request.json()
    raw_url = str(body.get("url") or control.BROWSER_HOME_URL).strip()
    try:
        url = await run_in_threadpool(url_security.validate_url, raw_url, "webpage")
    except URLSecurityError as exc:
        return _api_error(exc)
    result: dict
    try:
        await cdp.probe()
        await cdp.navigate(url)
        try:
            await run_in_threadpool(control.focus_window, "browser")
        except control.ControlError:
            pass
        result = {"ok": True, "mode": "kiosk"}
    except cdp.CDPError:
        try:
            result = await run_in_threadpool(control.open_browser_window, url)
        except control.ControlError as exc:
            return _api_error(exc)
    # Signed-in identity, best effort: only readable through DevTools
    # (kiosk) once the page has rendered its account button.
    result["signed_in_as"] = None
    if result.get("mode") == "kiosk":
        for _ in range(8):
            await asyncio.sleep(0.5)
            try:
                res = await cdp.evaluate(cdp.JS_GOOGLE_IDENTITY)
            except cdp.CDPError:
                break
            val = res.get("value")
            if val is None:
                continue
            result["signed_in_as"] = accounts_mod.mask_email(val) if val else "(account, email hidden)"
            break
    result["url"] = url
    db.audit(session["username"], "browser_open", url, deps.client_ip(request))
    return result


@app.post("/api/zoom/dialog")
async def api_zoom_dialog(request: Request):
    """list: pop-up dialogs Zoom has open; dismiss: close them via their
    own OK/Close button. Used by the program panel's canvas alert."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    action = str(body.get("action", "list"))
    if action not in ("list", "dismiss"):
        raise HTTPException(status_code=400, detail="action must be list or dismiss")
    if action == "dismiss":
        deps.require_rate_limit(session, "zoom_dialog_dismiss", max_calls=6, window_seconds=30)
    try:
        result = await run_in_threadpool(control.zoom_dialog, action)
    except control.ControlError as exc:
        return _api_error(exc)
    if action == "dismiss":
        db.audit(session["username"], "zoom_dialog_dismiss",
                 ", ".join(d.get("title", "") for d in result.get("dismissed", [])), deps.client_ip(request))
    return result


@app.post("/api/zoom/quit-to-slate")
async def api_zoom_quit_to_slate(request: Request):
    """"Reset Zoom window": stop zoom.service and put the slate on the
    canvas. The encoder is never touched."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "zoom_quit", max_calls=3, window_seconds=30)
    try:
        results = await run_in_threadpool(control.zoom_quit_to_slate)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "zoom_quit_to_slate", ip=deps.client_ip(request))
    return {"results": results}


@app.post("/api/window/focus")
async def api_window_focus(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "window_focus", max_calls=12, window_seconds=10)
    body = await request.json()
    which = str(body.get("which", ""))
    if which not in control.FOCUS_TARGETS:
        raise HTTPException(status_code=400, detail="which must be zoom or browser")
    try:
        result = await run_in_threadpool(control.focus_window, which)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "window_focus", which, deps.client_ip(request))
    return result


@app.post("/api/vnc/rate")
async def api_vnc_rate(request: Request):
    """fast while the preview is interactive, slow otherwise. The page
    calls this on toggle / visibility change; x11vnc keeps whatever was
    last set, so the client also sends `slow` on unload."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "vnc_rate", max_calls=12, window_seconds=10)
    body = await request.json()
    mode = str(body.get("mode", "slow"))
    if mode not in control.VNC_RATES:
        raise HTTPException(status_code=400, detail="mode must be fast or slow")
    try:
        result = await run_in_threadpool(control.set_vnc_rate, mode)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "vnc_rate", mode, deps.client_ip(request))
    return result


@app.post("/api/audio/selftest")
async def api_audio_selftest(request: Request):
    """End-to-end audio proof (tone -> sink -> meter + capture -> file).
    The script refuses to run while ffmpeg-stream is active; ~12s."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    deps.require_rate_limit(session, "audio_selftest", max_calls=2, window_seconds=60)
    try:
        result = await run_in_threadpool(control.audio_selftest)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], "audio_selftest", "ok" if result.get("ok") else "failed", deps.client_ip(request))
    return result


# ---------------------------------------------------------------- api: schedules

def _list_schedules() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY hour, minute").fetchall()
        return [dict(r) for r in rows]


@app.get("/api/schedules")
async def api_schedules_list(request: Request):
    deps.require_session_api(request)
    return _list_schedules()


@app.post("/api/schedules")
async def api_schedules_create(request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    action = body.get("action")
    if action not in ("go_live", "stop"):
        raise HTTPException(status_code=400, detail="action must be go_live or stop")
    hour, minute = int(body.get("hour", 0)), int(body.get("minute", 0))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=400, detail="invalid time")
    days = str(body.get("days_of_week", "mon,tue,wed,thu,fri,sat,sun"))
    source_id = body.get("source_id")
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (source_id, action, hour, minute, days_of_week, enabled) "
            "VALUES (?,?,?,?,?,1)",
            (source_id, action, hour, minute, days),
        )
        sid = cur.lastrowid
    scheduler.load_schedules()
    db.audit(session["username"], "schedule_create", f"{action} {hour:02d}:{minute:02d}", deps.client_ip(request))
    return {"id": sid}


@app.delete("/api/schedules/{schedule_id}")
async def api_schedules_delete(schedule_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    scheduler.load_schedules()
    db.audit(session["username"], "schedule_delete", str(schedule_id), deps.client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------- api: logs / errors / audit

@app.get("/api/logs/stream")
async def api_logs_stream(request: Request, unit: str):
    deps.require_session_api(request)
    if unit not in config.VISIBLE_UNITS:
        raise HTTPException(status_code=400, detail="unknown unit")

    async def gen():
        async for line in logs_mod.tail_for_unit(unit):
            yield f"data: {json.dumps(line)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.get("/api/logs/download")
async def api_logs_download(request: Request, unit: str):
    deps.require_session_api(request)
    if unit not in config.VISIBLE_UNITS:
        raise HTTPException(status_code=400, detail="unknown unit")
    redact = logs_mod.build_redactor()
    lines = []
    if unit in logs_mod.FILE_BACKED_UNITS:
        path = logs_mod.FILE_BACKED_UNITS[unit]
        if path.exists():
            lines = [redact(l) for l in path.read_text(errors="replace").splitlines()[-2000:]]
    else:
        argv = [
            logs_mod.JOURNALCTL, f"_SYSTEMD_USER_UNIT={unit}.service",
            f"_UID={control.zoombot_uid()}", "-n", "2000", "--no-pager", "-o", "short-iso",
        ]
        proc = subprocess.run(argv, capture_output=True, timeout=15)
        lines = [redact(l) for l in proc.stdout.decode(errors="replace").splitlines()]
    return PlainTextResponse("\n".join(lines), headers={
        "Content-Disposition": f'attachment; filename="{unit}.log.txt"'
    })


@app.get("/api/errors")
async def api_errors(request: Request):
    deps.require_session_api(request)
    return logs_mod.recent_errors()


@app.get("/api/audit")
async def api_audit(request: Request):
    deps.require_session_api(request)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- vnc websocket

@app.websocket("/vnc/ws")
@app.websocket("/ws/vnc")
async def vnc_ws(websocket: WebSocket):
    await vnc_proxy.proxy(websocket)


# ---------------------------------------------------------------- health / startup

@app.get("/health")
async def health():
    return {"ok": True}


@app.on_event("startup")
async def on_startup():
    db.init_db()
    scheduler.start()
    from . import zoom_web, telegram
    zoom_web.start_cleanfeed_heartbeat()
    telegram.start_telegram_poller()

