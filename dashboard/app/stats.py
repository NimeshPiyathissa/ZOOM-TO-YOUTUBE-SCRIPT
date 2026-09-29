"""System resource stats (pure /proc reads via psutil, no privilege
needed) and FFmpeg progress parsed from its log tail.

Everything here that reports on ffmpeg-stream takes the *same* already-
fetched control.unit_show() snapshot the caller used for the unit list,
rather than making its own separate `systemctl show` call - see
control.py's module note on why (the hero-card-vs-badge disagreement
incident)."""
from __future__ import annotations

import re
import time

import psutil

from . import config, logs
from .control import PHASE_FAILED, PHASE_LIVE, unit_last_error
from .logs import is_benign_line

_prev_net: tuple[int, float] | None = None

FRAME_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?bitrate=\s*([\d.]+)kbits/s.*?speed=\s*([\d.]+)x"
)
DUP_RE = re.compile(r"dup=(\d+)")
DROP_RE = re.compile(r"drop=(\d+)")

# How old the last write to ffmpeg.log is allowed to be before we refuse
# to call it "current" - this is the fix for the stale-cache incident
# (Stream health showed a 30-hour-old frame/fps/speed/bitrate snapshot as
# if it were live, because nothing ever checked the file's age or the
# unit's actual state before serving its last-known content).
FFMPEG_LOG_STALE_AFTER_SECONDS = 8


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


# Small rolling record of recent live speed readings, used only as a
# weak signal for the "CPU cannot sustain encode" Failed-state cause
# guess below - not shown anywhere as its own metric.
_SPEED_HISTORY_MAX = 10
_recent_speeds: list[float] = []


_encoder_proc: psutil.Process | None = None


def _get_encoder_cpu(pid: int | None) -> float | None:
    global _encoder_proc
    if not pid or pid <= 0:
        _encoder_proc = None
        return None
    try:
        if _encoder_proc is None or _encoder_proc.pid != pid:
            _encoder_proc = psutil.Process(pid)
            _encoder_proc.cpu_percent(interval=None)
            return None
        return round(_encoder_proc.cpu_percent(interval=None), 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        _encoder_proc = None
        return None


def ffmpeg_progress(show: dict) -> dict | None:
    """Returns live FFmpeg progress, or None if there's no reason to
    trust the log file as current: the unit must actually be LIVE (real
    PID, not just "the process object exists"), and the log's last write
    must be recent - a dead run's last-ever progress line must never be
    served as if it were happening now."""
    if show.get("phase") != PHASE_LIVE:
        return None
    if not config.FFMPEG_LOG.exists():
        return None
    try:
        st = config.FFMPEG_LOG.stat()
        age = time.time() - st.st_mtime
        if age > FFMPEG_LOG_STALE_AFTER_SECONDS:
            return None
        with open(config.FFMPEG_LOG, "r", errors="replace") as f:
            f.seek(max(0, st.st_size - 4000))
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
            _recent_speeds.append(speed)
            del _recent_speeds[:-_SPEED_HISTORY_MAX]
            return {
                "frame": int(m.group(1)),
                "fps": float(m.group(2)),
                "bitrate_kbps": float(m.group(3)),
                "speed": speed,
                "dup": int(dup.group(1)) if dup else 0,
                "drop": int(drop.group(1)) if drop else 0,
                "warning": speed < 1.0,
                "age_seconds": round(age, 1),
                "encoder_cpu": _get_encoder_cpu(show.get("main_pid")),
            }
    return None


# ---------------------------------------------------------------- restart-rate tracking

# In-memory only (resets if dashboard.service restarts) - this is a
# recent-trend indicator for the UI, not a permanent record. The
# permanent record is the audit log's watchdog_alert entries.
_RESTART_HISTORY_WINDOW_SECONDS = 300
_restart_samples: list[tuple[float, int]] = []


def _track_restart_rate(restart_count: int) -> int:
    """Records a sample and returns how many restarts happened in the
    last 5 minutes, from the delta of the monotonically-increasing
    NRestarts counter across the window - immune to the counter simply
    never resetting between polls."""
    now = time.time()
    _restart_samples.append((now, restart_count))
    cutoff = now - _RESTART_HISTORY_WINDOW_SECONDS
    while len(_restart_samples) > 1 and _restart_samples[0][0] < cutoff:
        _restart_samples.pop(0)
    return max(0, restart_count - _restart_samples[0][1])


# ---------------------------------------------------------------- Failed-state diagnostics

_PROGRESS_LINE_RE = re.compile(r"frame=\s*\d+.*speed=")
_ERRORISH_RE = re.compile(r"error|fail|refused|denied|not found|invalid|unable|cannot", re.I)


def last_ffmpeg_log_error() -> str | None:
    """Last line of ffmpeg.log that looks like a real error - skipping
    progress lines and the benign set above. ffmpeg's own diagnostics
    land here, not in the journal (stream.sh redirects them), which is
    why journalctl alone found nothing to show for a failed encoder."""
    if not config.FFMPEG_LOG.exists():
        return None
    try:
        size = config.FFMPEG_LOG.stat().st_size
        with open(config.FFMPEG_LOG, "r", errors="replace") as f:
            f.seek(max(0, size - 16000))
            tail = f.read()
    except OSError:
        return None
    for line in reversed(re.split(r"[\r\n]+", tail)):
        line = line.strip()
        if not line or _PROGRESS_LINE_RE.search(line) or is_benign_line(line):
            continue
        if _ERRORISH_RE.search(line):
            return line[:400]
    return None

# Best-effort, ordered (first match wins) - matched against the last
# redacted journal error line for the unit. These patterns are inferred
# from the incident this was built to catch and from FFmpeg/RTMP's
# documented error shapes; the "YouTube rejected the stream key" and
# "CPU cannot sustain encode" cases specifically have NOT been observed
# firsthand in this project (reproducing them safely would mean actually
# breaking the live stream key or starving the encoder), so treat them as
# reasonable guesses, not verified signatures - if you see one fire
# incorrectly, tighten the pattern.
_CAUSE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"command not found", re.I), "Script aborted before ffmpeg started"),
    (re.compile(r"\.env[^\n]*(No such file|line \d+:)", re.I), "Script aborted before ffmpeg started"),
    (re.compile(r"no such (sink|source)|zoom_out|Connection refused.*pulse|pa_context_connect", re.I),
     "Audio monitor missing"),
    (re.compile(r"forbidden|unauthorized|403|Server error|authentication.*fail", re.I),
     "YouTube rejected the stream key"),
]

_LOW_SPEED_CAUSE_THRESHOLD = 0.85


def _classify_cause(error_line: str | None) -> str | None:
    if error_line and not is_benign_line(error_line):
        for pattern, cause in _CAUSE_PATTERNS:
            if pattern.search(error_line):
                return cause
    if len(_recent_speeds) >= 3 and (sum(_recent_speeds) / len(_recent_speeds)) < _LOW_SPEED_CAUSE_THRESHOLD:
        return "CPU cannot sustain encode"
    return None


def stream_state(show: dict) -> dict:
    """The dashboard's one summary of ffmpeg-stream's state - `show` is
    the caller's single control.unit_show("ffmpeg-stream") snapshot,
    reused as-is so this can never disagree with the same unit's row in
    the units list."""
    result = {
        "phase": show["phase"],
        "active_state": show["active_state"],
        "sub_state": show["sub_state"],
        "uptime_seconds": show.get("uptime_seconds"),
        "restart_count": show.get("restart_count", 0),
        "main_pid": show.get("main_pid", 0),
        "restarts_last_5min": _track_restart_rate(show.get("restart_count", 0)),
    }
    if show["phase"] == PHASE_FAILED:
        redact = logs.build_redactor()
        raw_error = unit_last_error("ffmpeg-stream")
        if raw_error and is_benign_line(raw_error):
            raw_error = None
        # The journal only has what stream.sh printed before exec'ing
        # ffmpeg; ffmpeg's own error text is in ffmpeg.log.
        if not raw_error:
            raw_error = last_ffmpeg_log_error()
        result["last_error"] = redact(raw_error) if raw_error else None
        result["cause"] = _classify_cause(raw_error)
    return result


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
