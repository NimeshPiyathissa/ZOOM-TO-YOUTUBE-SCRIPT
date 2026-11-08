"""What each service's state MEANS, so an intentionally idle unit is not shown as a failure.

systemd only says active/inactive/failed. That is not enough for this stack: `ffmpeg-stream` is inactive whenever
nothing is being broadcast, `browser-source` is inactive whenever the active source is the desktop Zoom client, and
`zoom` is inactive whenever the active source is a web page. None of those is a fault. This module combines the raw
unit state (control.unit_show) with what the active source actually needs and returns one of a small closed set:

    active        running and doing its job            (green)
    on_air        the encoder is streaming             (scarlet LIVE pill)
    starting      coming up                            (amber)
    retrying      crash-looping / waiting to restart   (amber)
    error         failed                               (red)
    stopped       NOT running although the active source needs it     (red)
    idle          not running, and nothing needs it right now          (neutral)
    off_air       the encoder, not broadcasting - the normal resting state (neutral)
    not_configured  the unit file does not exist here  (neutral)
    disconnected  the unit is up but its heartbeat is stale (worker only)   (amber)

Nothing here talks to systemd by itself except system_services(), which does read-only `systemctl is-active` calls.
"""
from __future__ import annotations

import subprocess
import time

from . import config

LABELS = {
    "active": "Active", "on_air": "On air", "starting": "Starting", "retrying": "Restarting", "error": "Error",
    "stopped": "Stopped", "idle": "Idle", "off_air": "Off air", "not_configured": "Not configured",
    "disconnected": "Disconnected", "connecting": "Connecting", "unknown": "Unknown", "failed": "Failed",
}

TITLES = {
    "xvfb": "Virtual display", "openbox": "Window manager", "audio-setup": "Audio routing", "zoom": "Zoom client",
    "browser-source": "Browser source", "x11vnc": "Remote screen (VNC)", "novnc-proxy": "noVNC gateway",
    "ffmpeg-stream": "Encoder to YouTube",
}

# What the kiosk browser is doing for each source type that uses it (shown in its status reason).
_BROWSER_ROLE = {"zoom": " (Zoom web client for the active meeting)", "webpage": " (showing the active web page)",
                 "edumix": " (EduMix class page - view only, capture disabled)"}

# Units every source needs; if one of these is down something really is wrong.
ALWAYS_NEEDED = {"xvfb", "openbox", "audio-setup", "x11vnc", "novnc-proxy"}


def _result(kind: str, reason: str, **extra) -> dict:
    return {"kind": kind, "label": LABELS[kind], "reason": reason, **extra}


def classify_unit(show: dict, producer_unit: str | None, source_type: str | None = None) -> dict:
    """`show` is one control.unit_show() snapshot; `producer_unit` is control.producer_unit_for(active_source)."""
    unit = show.get("unit", "")
    phase = show.get("phase")
    active, sub = show.get("active_state", ""), show.get("sub_state", "")

    if show.get("unit_file_state") == "not-found":
        return _result("not_configured", f"{unit}.service is not installed on this machine.")
    if phase == "FAILED":
        return _result("error", f"{unit} failed (result: {show.get('result') or 'unknown'}). See Logs.")
    if phase == "RECONNECTING":
        return _result("retrying", f"{unit} is waiting to restart after a failure.")
    if phase == "STARTING":
        return _result("starting", f"{unit} is starting.")
    if phase == "LIVE":
        if unit == "ffmpeg-stream":
            return _result("on_air", "The encoder is streaming to YouTube.")
        if active == "active" and sub == "exited":
            return _result("active", "One-shot setup finished successfully; nothing keeps running by design.")
        if unit == "browser-source":
            what = _BROWSER_ROLE.get(source_type if producer_unit == "browser-source" else "", "")
            return _result("active", f"Browser source is running{what}. Running is not the same as on air - only Go Live broadcasts.")
        if unit == "zoom" and producer_unit == "zoom":
            return _result("active", "The Zoom desktop client is running. See the Zoom meeting row for whether it is in the meeting.")
        return _result("active", f"{TITLES.get(unit, unit)} is running.")

    # inactive / dead from here on
    if unit == "ffmpeg-stream":
        return _result("off_air", "Not broadcasting. This is normal until you press Go Live.")
    if unit == "browser-source":
        if producer_unit == "browser-source":
            what = _BROWSER_ROLE.get(source_type or "", "")
            return _result("stopped", f"The active source needs the browser{what}, but it is not running. Press Join / Start "
                                      "on the source; nothing goes on air until Go Live.")
        why = "no source is selected" if producer_unit is None and not source_type else "the active source does not use it"
        return _result("idle", f"Not needed: {why}.")
    if unit == "zoom":
        if producer_unit == "zoom":
            return _result("stopped", "The active source is a Zoom meeting but the Zoom desktop client is not running "
                                      "(not joined, left, or it exited). Press Join to start it; joining never goes on air.")
        if source_type == "zoom" and producer_unit == "browser-source":
            return _result("idle", "Not needed: this Zoom meeting is joined in the browser (web client), not the desktop app.")
        return _result("idle", "Not needed: Zoom is not the active source.")
    if unit in ALWAYS_NEEDED:
        return _result("stopped", f"{TITLES.get(unit, unit)} is required by every source but is not running.")
    return _result("idle", "Not running.")


def annotate_units(units: list[dict], producer_unit: str | None, source_type: str | None) -> list[dict]:
    """Adds `status` {kind,label,reason} and `title` to every unit row (in place, returns the list)."""
    for u in units:
        u["title"] = TITLES.get(u.get("unit", ""), u.get("unit", ""))
        u["status"] = classify_unit(u, producer_unit, source_type)
    return units


# ---------------------------------------------------------------- system services (dashboard, caddy, worker)

_CACHE: dict = {"at": 0.0, "value": None}
CACHE_SECONDS = 10.0
WORKER_STALE_SECONDS = 30.0


def _is_active(unit: str) -> str:
    try:
        out = subprocess.run(["systemctl", "is-active", unit], capture_output=True, timeout=4)
        return out.stdout.decode(errors="replace").strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unavailable"


def classify_system(name: str, systemd_state: str, *, job: dict | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if systemd_state == "unavailable":
        return _result("not_configured", "systemd is not available here (local development).")
    if systemd_state == "failed":
        return _result("error", f"{name} failed.")
    if systemd_state in ("activating", "reloading"):
        return _result("starting", f"{name} is starting.")
    if systemd_state != "active":
        if name == "edumix-worker" and not config.EDUMIX_WORKER_EXTERNAL:
            return _result("not_configured", "The worker runs inside the dashboard in this setup.")
        return _result("stopped", f"{name} is {systemd_state}.")
    if name == "edumix-worker":
        if job is None or job.get("completed_at") or job.get("desired") != "running":
            return _result("idle", "Running and waiting: no EduMix class is open.")
        hb = job.get("heartbeat")
        if hb is None or now - float(hb) > WORKER_STALE_SECONDS:
            return _result("disconnected", "The worker is running but has not reported for over 30 s.")
        return _result("active", "Supervising the open EduMix class.")
    return _result("active", f"{name} is running.")


# ---------------------------------------------------------------- pipeline indicators (meeting, YouTube, last error)

_ZOOM: dict = {"at": 0.0, "verdict": None}
ZOOM_FRESH_SECONDS = 90.0


def remember_zoom(verdict: dict | None, now: float | None = None) -> None:
    """Called whenever a Zoom status check ran anyway (the Zoom page polls it); /api/state never triggers one itself."""
    _ZOOM.update(at=time.time() if now is None else now, verdict=verdict)


def pipeline_indicators(broadcast_state: dict | None, now: float | None = None) -> list[dict]:
    """Meeting state and YouTube/encoder-session state, as rows shaped like system services. The meeting row is only as
    fresh as the last Zoom status check: when it is old it says UNKNOWN instead of repeating a stale answer."""
    now = time.time() if now is None else now
    rows = []
    v = _ZOOM["verdict"]
    if v is None or now - _ZOOM["at"] > ZOOM_FRESH_SECONDS:
        zs = _result("unknown", "Meeting state has not been checked recently. Open the Zoom page to refresh it.")
    else:
        kind = {"in_meeting": "active", "not_running": "idle", "not_joined": "idle", "connecting": "connecting", "prejoin": "connecting",
                "waiting_room": "connecting", "unknown": "unknown"}.get(v.get("status"), "error" if v.get("status") else "unknown")
        zs = _result(kind, f"{v.get('detail') or v.get('status')} (confidence: {v.get('confidence', 'n/a')}, {int(now - _ZOOM['at'])}s ago)")
    rows.append({"unit": "zoom-meeting", "title": "Zoom meeting", "systemd": "", "scope": "derived", "status": zs})
    b = broadcast_state or {}
    st = b.get("state")
    err = b.get("last_error") or {}
    if st == "ERROR":
        ys = _result("failed", "The broadcast stopped unexpectedly and was NOT restarted: %s" % (err.get("reason") or "see Logs"))
    elif st == "LIVE" and b.get("adopted"):
        ys = _result("on_air", "Streaming to YouTube, but this encoder was started before Go Live authorization existed "
                               "(no session record). Stop it and press Go Live to bring it under authorization.")
    elif st == "LIVE":
        ys = _result("on_air", "Streaming to YouTube; started by an authorized Go Live.")
    elif st in ("STARTING", "STOPPING"):
        ys = _result("starting", f"Broadcast is {st.lower()}.")
    elif st in ("READY", "OFFLINE"):
        ready = "The source is ready. " if st == "READY" else "No source is ready yet. "
        pending = " A Go Live confirmation is open (it expires after 60 s if not confirmed)." if b.get("grants_outstanding") else ""
        ys = _result("off_air", f"Not broadcasting. {ready}Nothing goes on air until you press Go Live and confirm.{pending}")
    else:
        ys = _result("unknown", "Broadcast state unavailable.")
    rows.append({"unit": "youtube-broadcast", "title": "YouTube broadcast", "systemd": "", "scope": "derived", "status": ys})
    return rows


def system_services(now: float | None = None) -> list[dict]:
    """dashboard / caddy / edumix-worker. Cached for 10 s: this is read on every state poll."""
    now = time.time() if now is None else now
    if _CACHE["value"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["value"]
    try:
        from . import jobs
        job = jobs.current()
    except Exception:
        job = None
    rows = []
    for name, title in (("dashboard", "Dashboard"), ("caddy", "Web server (Caddy)"), ("edumix-worker", "EduMix worker")):
        state = "active" if name == "dashboard" else _is_active(name)     # this process is the dashboard
        row = {"unit": name, "title": title, "systemd": state, "scope": "system"}
        row["status"] = classify_system(name, state, job=job, now=now)
        rows.append(row)
    _CACHE.update(at=now, value=rows)
    return rows
