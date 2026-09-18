"""Generic source model (Change 1): a saved source is one of
zoom / webpage / direct, each with its own URL-shape and options. This
replaces `profiles` (Zoom-only) as the thing Controls/Configuration/
Overview show and switch between; `profiles` itself is left untouched -
see app/cli.py's migrate-sources command for the one-time copy."""
from __future__ import annotations

import json
import sqlite3
import time
from urllib.parse import urlsplit

from . import config, db, url_security, zoomlink
from .env_store import ValidationError

MEDIA_EXTENSIONS = (".m3u8", ".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm")


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["options"] = json.loads(d["options"]) if d["options"] else {}
    except json.JSONDecodeError:
        d["options"] = {}
    if d["type"] == "zoom":
        d["link_kind"] = zoomlink.classify(d["url"])["kind"]
        d["join_ready"] = bool(effective_zoom_join_url(d))
    return d


def effective_zoom_join_url(source: dict) -> str | None:
    """The link the Zoom client will actually be handed: the saved
    per-registrant link if there is one, else the source URL itself if
    it is a joinable (meeting/personal) link. None for a registration
    page with no personal link saved yet."""
    join_url = (source.get("options") or {}).get("join_url") or ""
    if join_url and zoomlink.classify(join_url)["kind"] in ("meeting", "personal"):
        return join_url
    if zoomlink.classify(source["url"])["kind"] in ("meeting", "personal"):
        return source["url"]
    return None


def list_sources() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [_row_to_dict(r) for r in rows]


def get_source(source_id: int) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_active_source() -> dict | None:
    sid = db.get_setting("active_source_id")
    if not sid:
        return None
    return get_source(int(sid))


def set_active_source_id(source_id: int | None) -> None:
    db.set_setting("active_source_id", str(source_id) if source_id is not None else "")


def detect_type(url: str) -> str:
    """Best-effort suggestion for the "Add source" auto-detect UX. Never
    authoritative on its own - the caller always still passes an explicit
    `type` (possibly overridden by the admin) to create_source/validate."""
    url = (url or "").strip()
    if not url:
        return "webpage"
    parts = urlsplit(url if "://" in url else f"https://{url}")
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower()

    if "zoom.us" in host:
        return "zoom"
    if scheme in ("rtmp", "rtmps", "srt"):
        return "direct"
    if path.endswith(MEDIA_EXTENSIONS):
        return "direct"
    return "webpage"


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ValidationError("Source name must be 1-64 characters")
    return name


def _validate_zoom_options(options: dict) -> dict:
    passcode = str(options.get("passcode", "")).strip()
    bot_name = str(options.get("bot_name", "Stream Bot")).strip()
    signin_mode = str(options.get("signin_mode", "guest")).strip().lower()
    join_url = str(options.get("join_url", "")).strip()
    if not bot_name or len(bot_name) > 64 or any(c in bot_name for c in "\r\n"):
        raise ValidationError("Bot name must be 1-64 characters, no newlines")
    if signin_mode not in config.ZOOM_SIGNIN_MODES:
        raise ValidationError(f"signin_mode must be one of {sorted(config.ZOOM_SIGNIN_MODES)}")
    if join_url:
        # The personal (tk=) link saved after completing a registration
        # form by hand - see zoomlink.py.
        try:
            zoomlink.validate_joinable(join_url)
        except zoomlink.ZoomLinkError as exc:
            raise ValidationError(f"Saved join link: {exc}")
    return {"passcode": passcode, "bot_name": bot_name, "signin_mode": signin_mode, "join_url": join_url}


def _validate_webpage_options(options: dict) -> dict:
    lo, hi = config.WEBPAGE_ZOOM_RANGE
    try:
        zoom_level = float(options.get("zoom_level", 1.0))
    except (TypeError, ValueError):
        raise ValidationError("zoom_level must be a number")
    if not (lo <= zoom_level <= hi):
        raise ValidationError(f"zoom_level must be between {lo} and {hi}")

    rlo, rhi = config.WEBPAGE_RELOAD_RANGE_SECONDS
    try:
        reload_seconds = int(options.get("reload_seconds", 0))
    except (TypeError, ValueError):
        raise ValidationError("reload_seconds must be an integer")
    if not (rlo <= reload_seconds <= rhi):
        raise ValidationError(f"reload_seconds must be between {rlo} and {rhi}")

    return {
        "zoom_level": zoom_level,
        "reload_seconds": reload_seconds,
        "click_to_start": bool(options.get("click_to_start", False)),
    }


def _validate_direct_options(options: dict) -> dict:
    mode = str(options.get("mode", "reencode")).strip().lower()
    if mode not in config.DIRECT_MODES:
        raise ValidationError(f"mode must be one of {sorted(config.DIRECT_MODES)}")
    return {
        "mode": mode,
        "loop": bool(options.get("loop", False)),
        "reconnect": bool(options.get("reconnect", True)),
    }


_OPTION_VALIDATORS = {
    "zoom": _validate_zoom_options,
    "webpage": _validate_webpage_options,
    "direct": _validate_direct_options,
}


def validate_source(type_: str, url: str, options: dict) -> tuple[str, dict]:
    """Returns (validated_url, validated_options) or raises ValidationError
    / url_security.URLSecurityError. Does NOT hit the network for webpage/
    direct URLs beyond DNS resolution (done inside validate_url)."""
    if type_ not in config.SOURCE_TYPES:
        raise ValidationError(f"type must be one of {sorted(config.SOURCE_TYPES)}")
    if type_ == "zoom":
        # A registration page is a valid thing to SAVE (the flow completes
        # it later and stores the personal link in options.join_url); it
        # just isn't joinable by itself - see effective_zoom_join_url().
        kind = zoomlink.classify(url)["kind"]
        if kind not in ("meeting", "personal", "registration"):
            raise ValidationError(
                "Not a recognized Zoom link. Expected a join link (zoom.us/j/<id>), a personal "
                "join link (zoom.us/w/<id>?tk=...), or a registration page (zoom.us/webinar/register/...)"
            )
        if kind != "registration":
            try:
                zoomlink.validate_joinable(url)
            except zoomlink.ZoomLinkError as exc:
                raise ValidationError(str(exc))
    else:
        try:
            url = url_security.validate_url(url, type_)
        except url_security.URLSecurityError as exc:
            raise ValidationError(str(exc))
    validated_options = _OPTION_VALIDATORS[type_](options or {})
    return url.strip(), validated_options


def _validate_account_id(account_id) -> int | None:
    if account_id in (None, "", 0, "0"):
        return None
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise ValidationError("account_id must be an integer")
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            raise ValidationError("That account no longer exists")
    return account_id


def create_source(name: str, type_: str, url: str, options: dict, account_id=None) -> int:
    name = _validate_name(name)
    url, options = validate_source(type_, url, options)
    account_id = _validate_account_id(account_id)
    now = time.time()
    with db.get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO sources (name, type, url, options, created_at, updated_at, account_id) VALUES (?,?,?,?,?,?,?)",
                (name, type_, url, json.dumps(options), now, now, account_id),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(f"A source named {name!r} already exists")
        return cur.lastrowid


def update_source(source_id: int, name: str, type_: str, url: str, options: dict, account_id=None) -> None:
    name = _validate_name(name)
    url, options = validate_source(type_, url, options)
    account_id = _validate_account_id(account_id)
    with db.get_conn() as conn:
        try:
            conn.execute(
                "UPDATE sources SET name=?, type=?, url=?, options=?, updated_at=?, account_id=? WHERE id=?",
                (name, type_, url, json.dumps(options), time.time(), account_id, source_id),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(f"A source named {name!r} already exists")


def set_zoom_join_url(source_id: int, join_url: str) -> dict:
    """Saves the personal (tk=) link obtained by completing a registration
    form by hand. Returns the refreshed source."""
    s = get_source(source_id)
    if not s or s["type"] != "zoom":
        raise ValidationError("Not a Zoom source")
    join_url = (join_url or "").strip()
    try:
        info = zoomlink.validate_joinable(join_url)
    except zoomlink.ZoomLinkError as exc:
        raise ValidationError(str(exc))
    if s["url"] and zoomlink.classify(s["url"]).get("meeting_id") and info["meeting_id"] \
            and zoomlink.classify(s["url"])["meeting_id"] != info["meeting_id"]:
        raise ValidationError("That join link is for a different meeting ID than this source")
    options = dict(s["options"]); options["join_url"] = join_url
    with db.get_conn() as conn:
        conn.execute("UPDATE sources SET options=?, updated_at=? WHERE id=?",
                     (json.dumps(options), time.time(), source_id))
    return get_source(source_id)


def set_account(source_id: int, account_id) -> None:
    account_id = _validate_account_id(account_id)
    with db.get_conn() as conn:
        conn.execute("UPDATE sources SET account_id=?, updated_at=? WHERE id=?", (account_id, time.time(), source_id))


def delete_source(source_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    if db.get_setting("active_source_id") == str(source_id):
        set_active_source_id(None)
