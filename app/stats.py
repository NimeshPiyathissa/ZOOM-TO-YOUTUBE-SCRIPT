"""System resource stats (pure /proc reads via psutil, no privilege
needed) and FFmpeg progress parsed from its log tail."""
from __future__ import annotations

import re
import time

import psutil

from . import config
from .control import unit_show

_prev_net: tuple[int, float] | None = None

FRAME_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?bitrate=\s*([\d.]+)kbits/s.*?speed=\s*([\d.]+)x"
)
DUP_RE = re.compile(r"dup=(\d+)")
DROP_RE = re.compile(r"drop=(\d+)")


def system_stats() -> dict:
    global _prev_net
    cpu_per_core = psutil.cpu_percent(percpu=True)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = psutil.getloadavg()
    net = psutil.net_io_counters()
    now = time.time()
    upload_kbps = None
    if _prev_net is not None:
        prev_bytes, prev_time = _prev_net
        dt = now - prev_time
        if dt > 0.5:
            upload_kbps = ((net.bytes_sent - prev_bytes) * 8 / 1000) / dt
    _prev_net = (net.bytes_sent, now)
    return {
        "cpu_per_core": cpu_per_core,
        "cpu_avg": round(sum(cpu_per_core) / len(cpu_per_core), 1) if cpu_per_core else 0,
        "mem_used_gb": round(mem.used / 1e9, 2),
        "mem_total_gb": round(mem.total / 1e9, 2),
        "mem_percent": mem.percent,
        "disk_used_gb": round(disk.used / 1e9, 2),
        "disk_total_gb": round(disk.total / 1e9, 2),
        "disk_percent": disk.percent,
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "upload_kbps": round(upload_kbps, 1) if upload_kbps is not None else None,
    }


def ffmpeg_progress() -> dict | None:
    if not config.FFMPEG_LOG.exists():
        return None
    try:
        size = config.FFMPEG_LOG.stat().st_size
        with open(config.FFMPEG_LOG, "r", errors="replace") as f:
            f.seek(max(0, size - 4000))
            tail = f.read()
    except OSError:
        return None
    for line in reversed(re.split(r"[\r\n]+", tail)):
        if "frame=" in line and "speed=" in line:
            m = FRAME_RE.search(line)
            if not m:
                continue
            dup = DUP_RE.search(line)
            drop = DROP_RE.search(line)
            speed = float(m.group(4))
            return {
                "frame": int(m.group(1)),
                "fps": float(m.group(2)),
                "bitrate_kbps": float(m.group(3)),
                "speed": speed,
                "dup": int(dup.group(1)) if dup else 0,
                "drop": int(drop.group(1)) if drop else 0,
                "warning": speed < 1.0,
            }
    return None


def stream_state() -> dict:
    show = unit_show("ffmpeg-stream")
    active = show["active_state"]
    if active == "active":
        state = "LIVE"
    elif active == "failed" or show.get("result") not in ("", "success"):
        state = "ERROR"
    else:
        state = "STOPPED"
    return {
        "state": state,
        "uptime_seconds": show.get("uptime_seconds"),
        "restart_count": show.get("restart_count", 0),
    }


# How fresh a page-load marker has to be to still count as "loaded" - the
# marker is (re)written once per successful load/reload, not on a timer,
# so this just needs to outlive the longest configured reload interval by
# a comfortable margin; browser-source.sh also rewrites it hourly as a
# keepalive so a genuinely-stuck page eventually reads as stale.
BROWSER_LOADED_STALE_AFTER_SECONDS = 2 * 3600


def webpage_health() -> dict:
    """Best-effort "did the page load" signal for a webpage source. The
    dashboard user can read this directly (no sudo) - zoom-stream/logs is
    group-readable and dashboard is in the zoombot group, same as the
    ffmpeg.log/zoom.log tailing in app/logs.py."""
    try:
        mtime = config.BROWSER_LOADED_MARKER.stat().st_mtime
    except OSError:
        return {"page_loaded": False, "loaded_at": None}
    fresh = (time.time() - mtime) < BROWSER_LOADED_STALE_AFTER_SECONDS
    return {"page_loaded": fresh, "loaded_at": mtime if fresh else None}
