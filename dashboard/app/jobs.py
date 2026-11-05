"""Durable EduMix source-job state (SQLite), shared by the dashboard (control + monitoring) and the source worker.

The dashboard writes the operator's *intent* (open / stop); the worker (app/edumix_worker.py) owns the *execution*
and publishes health. Nothing here starts a process or an encoder, and the broadcast flag stays 0 while
config.EDUMIX_CAPTURE_ENABLED is False. Stores a URL *path* only (a class link's query string can carry tokens) and
a status summary - never cookies, OTPs or stream keys.
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import urlsplit

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS edumix_jobs (
    id             TEXT PRIMARY KEY,
    class_path     TEXT NOT NULL,
    desired        TEXT NOT NULL CHECK (desired IN ('running','stopped')),
    resume_on_boot INTEGER NOT NULL DEFAULT 0,
    broadcast      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    stopped_by     TEXT,
    completed_at   REAL,
    worker_pid     INTEGER,
    heartbeat      REAL,
    state          TEXT,
    status_json    TEXT,
    last_error     TEXT
);
"""

HEARTBEAT_STALE_SECONDS = 30.0
_COLS = ("id", "class_path", "desired", "resume_on_boot", "broadcast", "created_at", "updated_at", "stopped_by",
         "completed_at", "worker_pid", "heartbeat", "state", "status_json", "last_error")


def _ensure() -> None:
    with db.get_conn() as c:
        c.executescript(SCHEMA)


def _row(r) -> dict | None:
    if r is None:
        return None
    d = {k: r[k] for k in _COLS}
    d["resume_on_boot"], d["broadcast"] = bool(d["resume_on_boot"]), bool(d["broadcast"])
    try:
        d["status"] = json.loads(d.pop("status_json") or "null")
    except ValueError:
        d["status"] = None
    return d


def current() -> dict | None:
    """The newest job (there is one EduMix source at a time)."""
    _ensure()
    with db.get_conn() as c:
        return _row(c.execute("SELECT * FROM edumix_jobs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone())


def get(job_id: str) -> dict | None:
    _ensure()
    with db.get_conn() as c:
        return _row(c.execute("SELECT * FROM edumix_jobs WHERE id=?", (job_id,)).fetchone())


def open_job(class_url: str, *, resume_on_boot: bool = False, now: float | None = None) -> dict:
    """Operator opened a class: close out any previous job and create a new running one. broadcast is always 0
    while capture is disabled (a source job never implies a broadcast)."""
    _ensure()
    now = time.time() if now is None else now
    path = urlsplit(class_url).path or "/"
    jid = "job_" + secrets.token_hex(6)
    with db.get_conn() as c:
        c.execute("UPDATE edumix_jobs SET desired='stopped', completed_at=COALESCE(completed_at, ?), updated_at=? "
                  "WHERE completed_at IS NULL", (now, now))
        c.execute("INSERT INTO edumix_jobs (id, class_path, desired, resume_on_boot, broadcast, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (jid, path, "running", int(resume_on_boot), 0, now, now))
    return get(jid)


def request_stop(by: str = "operator", now: float | None = None) -> dict | None:
    """Explicit stop: the worker must not bring it back, and a reboot must not resume it."""
    job = current()
    if not job:
        return None
    now = time.time() if now is None else now
    with db.get_conn() as c:
        c.execute("UPDATE edumix_jobs SET desired='stopped', stopped_by=?, resume_on_boot=0, completed_at=?, updated_at=? "
                  "WHERE id=?", (by, now, now, job["id"]))
    return get(job["id"])


def heartbeat(job_id: str, pid: int, state: str, status: dict | None, error: str | None = None,
              now: float | None = None) -> None:
    now = time.time() if now is None else now
    summary = None
    if status is not None:       # keep only non-sensitive fields
        summary = json.dumps({k: status.get(k) for k in ("state", "level", "label", "needs_human", "page", "player", "reconnect")},
                             default=str)
    with db.get_conn() as c:
        c.execute("UPDATE edumix_jobs SET worker_pid=?, heartbeat=?, state=?, status_json=?, last_error=?, updated_at=? "
                  "WHERE id=?", (pid, now, state, summary, (error or "")[:300] or None, now, job_id))


def worker_alive(job: dict | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(job and job.get("heartbeat") and now - job["heartbeat"] <= HEARTBEAT_STALE_SECONDS)


def recovery_decision(job: dict | None) -> tuple[bool, str]:
    """Documented VPS-reboot policy (docs/edumix-vps.md). A job is resumed ONLY if every condition holds:
    the operator left it running, opted in to resume-on-boot, never stopped/completed it, and it is a
    source-only job (a broadcast is never resumed automatically)."""
    if not job:
        return False, "no job"
    if job["desired"] != "running":
        return False, "operator stopped it"
    if job["completed_at"] is not None or job["stopped_by"]:
        return False, "job is completed"
    if not job["resume_on_boot"]:
        return False, "resume-on-boot not enabled for this job"
    if job["broadcast"]:
        return False, "broadcasts are never resumed automatically"
    return True, "eligible"


SETTING_BOOT = "edumix_worker_boot_marker"


def apply_boot_policy(boot_marker: str, now: float | None = None) -> str:
    """Called once when the worker starts. A crash/systemd restart (same boot marker) adopts the job unchanged. A
    different marker means the machine rebooted: the job is resumed only if recovery_decision() allows it, otherwise it
    is closed (never silently resurrected - the operator opens the class again). Returns what happened."""
    now = time.time() if now is None else now
    last = db.get_setting(SETTING_BOOT)
    db.set_setting(SETTING_BOOT, str(boot_marker))
    job = current()
    if last is None or last == str(boot_marker):
        return "adopted" if job and job["desired"] == "running" and job["completed_at"] is None else "idle"
    if not job or job["desired"] != "running" or job["completed_at"] is not None:
        return "idle"
    ok, why = recovery_decision(job)
    if ok:
        return "resumed"
    with db.get_conn() as c:
        c.execute("UPDATE edumix_jobs SET desired='stopped', stopped_by='reboot-policy', completed_at=?, updated_at=?, "
                  "last_error=? WHERE id=?", (now, now, ("not resumed after restart: " + why)[:300], job["id"]))
    return "closed: " + why
