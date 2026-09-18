from __future__ import annotations

import json
import pathlib
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
from . import preview, stats, scheduler, vnc_proxy
from . import sources as sources_mod
from . import probe as probe_mod
from . import url_security
from .url_security import URLSecurityError

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

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
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self'; "
        # blob: is required for the Overview preview thumbnail (overview.js
        # fetches a JPEG and shows it via URL.createObjectURL), a JS-created
        # same-origin object URL, not attacker-controllable remote content.
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:; "
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
        "units": config.VISIBLE_UNITS, "sources": sources_mod.list_sources(),
        "active_source_id": active["id"] if active else None,
    })


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
        "cfg": env_store.masked_view(), "sources": sources_mod.list_sources(),
        "active_source_id": active["id"] if active else None,
        "presets": config.RESOLUTION_PRESETS,
        "auto_recovery": db.get_setting("auto_recovery", "off"),
        "webhook_configured": bool(db.get_setting("webhook_url", "")),
        "zoom_account": zoom_account,
        "signin_modes": sorted(config.ZOOM_SIGNIN_MODES),
        "direct_modes": sorted(config.DIRECT_MODES),
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
    return templates.TemplateResponse("vnc.html", {"request": request, "username": session["username"]})


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request):
    session = _require_page(request)
    if isinstance(session, RedirectResponse):
        return session
    return templates.TemplateResponse("schedule.html", {
        "request": request, "csrf_token": session["csrf_token"], "username": session["username"],
        "sources": sources_mod.list_sources(), "schedules": _list_schedules(),
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
    for unit in config.VISIBLE_UNITS:
        try:
            show = control.unit_show(unit)
        except control.ControlError as exc:
            show = {"unit": unit, "active_state": "unknown", "sub_state": "", "error": str(exc)}
        units.append(show)

    active_source = sources_mod.get_active_source()
    active_source_view = None
    source_health = None
    if active_source:
        active_source_view = {
            "id": active_source["id"], "name": active_source["name"], "type": active_source["type"],
            "url": active_source["url"], "url_truncated": _truncate_url(active_source["url"]),
        }
        if active_source["type"] == "webpage":
            source_health = stats.webpage_health()

    return {
        "stream": stats.stream_state(),
        "ffmpeg": stats.ffmpeg_progress(),
        "system": stats.system_stats(),
        "units": units,
        "active_source": active_source_view,
        "producer_unit": config.PRODUCER_UNITS.get(active_source["type"]) if active_source else None,
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
    verb = {"go-live": "start", "stop": "stop", "restart": "restart"}.get(action)
    if not verb:
        raise HTTPException(status_code=400, detail="invalid action")
    try:
        result = control.unit_action("ffmpeg-stream", verb)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], f"stream_{action}", ip=deps.client_ip(request))
    return result


@app.post("/api/zoom/{action}")
async def api_zoom_action(action: str, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    verb = {"join": "start", "leave": "stop", "rejoin": "restart"}.get(action)
    if not verb:
        raise HTTPException(status_code=400, detail="invalid action")
    try:
        result = control.unit_action("zoom", verb)
    except control.ControlError as exc:
        return _api_error(exc)
    db.audit(session["username"], f"zoom_{action}", ip=deps.client_ip(request))
    return result


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
    return {"sources": sources_mod.list_sources(), "active_id": active["id"] if active else None}


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
            body.get("url", ""), body.get("options", {}),
        )
    except (env_store.ValidationError, URLSecurityError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "source_create", body.get("name", ""), deps.client_ip(request))
    return {"id": sid}


@app.put("/api/sources/{source_id}")
async def api_sources_update(source_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    body = await request.json()
    try:
        sources_mod.update_source(
            source_id, body.get("name", ""), body.get("type", ""),
            body.get("url", ""), body.get("options", {}),
        )
    except (env_store.ValidationError, URLSecurityError) as exc:
        return _api_error(exc)
    db.audit(session["username"], "source_update", str(source_id), deps.client_ip(request))
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
async def api_sources_delete(source_id: int, request: Request):
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    sources_mod.delete_source(source_id)
    db.audit(session["username"], "source_delete", str(source_id), deps.client_ip(request))
    return {"ok": True}


@app.post("/api/sources/{source_id}/switch")
async def api_sources_switch(source_id: int, request: Request):
    """Switch-while-live = quick reconnect (see the plan this was built
    from): stop the encoder, swap the producer, start the encoder again.
    A few seconds of YouTube-side buffering, same as "Restart encoder"."""
    session = deps.require_session_api(request)
    deps.require_csrf(request, session)
    s = sources_mod.get_source(source_id)
    if not s:
        raise HTTPException(status_code=404, detail="source not found")
    try:
        results = await run_in_threadpool(control.start_source, s)
    except control.ControlError as exc:
        return _api_error(exc)
    sources_mod.set_active_source_id(source_id)
    db.audit(session["username"], "source_switch", s["name"], deps.client_ip(request))
    return {"results": results}


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
