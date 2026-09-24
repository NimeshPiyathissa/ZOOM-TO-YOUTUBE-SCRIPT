"""Generic source model (Change 1): a saved source is one of
zoom / webpage / direct, each with its own URL-shape and options. This
replaces `profiles` (Zoom-only) as the thing Controls/Configuration/
Overview show and switch between; `profiles` itself is left untouched -
see app/cli.py's migrate-sources command for the one-time copy."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from urllib.parse import urlsplit

from . import config, db, url_security, zoomlink
from .env_store import ValidationError

MEDIA_EXTENSIONS = (".m3u8", ".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _row_to_dict(row) -> dict:
    """The INTERNAL row: full URL and options, secrets included. Only
    control/scheduler code may consume this; anything that leaves the
    process (API, template, audit) goes through public_view()."""
    d = dict(row)
    try:
        d["options"] = json.loads(d["options"]) if d["options"] else {}
    except json.JSONDecodeError:
        d["options"] = {}
    if d["type"] == "zoom":
        info = zoomlink.classify(d["url"])
        d["link_kind"] = info["kind"]
        d["join_ready"] = bool(effective_zoom_join_url(d))
        d["meeting_id"] = info["meeting_id"] or zoomlink.classify(d["options"].get("join_url") or "")["meeting_id"]
        d["missing"] = zoom_missing(d)
        d["warnings"] = zoom_warnings(d)
    return d


ZOOM_SECRET_OPTION_KEYS = ("passcode", "join_url", "registrant_email")


def public_view(d: dict | None) -> dict | None:
    """What the browser and audit log get: pwd=/tk= redacted in every
    URL, the passcode replaced by has_passcode/passcode_masked, the
    personal join link by has_join_url. reveal_secrets() is the one
    audited way back."""
    if d is None:
        return None
    v = dict(d)
    v["url"] = zoomlink.redact_url(d["url"])
    if d["type"] == "zoom":
        from . import accounts as accounts_mod  # local: avoid a module-level cycle (accounts -> control)
        o = dict(d.get("options") or {})
        pc = o.pop("passcode", "") or ""
        ju = o.pop("join_url", "") or ""
        re_ = o.pop("registrant_email", "") or ""
        o["has_passcode"] = bool(pc) or zoomlink.classify(d["url"])["has_pwd"] or zoomlink.classify(ju)["has_pwd"]
        o["passcode_masked"] = ("\u2022" * min(max(len(pc), 4), 8)) if pc else ""
        o["has_join_url"] = bool(ju)
        o["join_url_redacted"] = zoomlink.redact_url(ju) if ju else ""
        o["registrant_email_masked"] = accounts_mod.mask_email(re_) if re_ else ""
        v["options"] = o
        v["meeting_id_formatted"] = zoomlink.format_meeting_id(d.get("meeting_id"))
    return v


def zoom_missing(d: dict) -> list[str]:
    """Exactly what stops this Zoom source from joining, in the operator's
    words. Empty list = joinable now."""
    missing: list[str] = []
    kind = zoomlink.classify(d["url"])["kind"]
    o = d.get("options") or {}
    if kind == "registration" and not o.get("join_url"):
        missing.append("the personal join link Zoom issues after you register")
    elif kind == "vanity":
        missing.append("a resolved join link for this personal room (re-paste it on the Zoom page)")
    elif kind not in ("meeting", "personal", "registration"):
        missing.append("a valid Zoom join link or meeting ID")
    if o.get("signin_mode") == "google" and not d.get("account_id"):
        missing.append("a Google account to join with (or switch to guest)")
    return missing


def _account_email(account_id) -> str | None:
    if not account_id:
        return None
    with db.get_conn() as conn:
        row = conn.execute("SELECT email FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row["email"] if row else None


def zoom_warnings(d: dict) -> list[str]:
    """Non-blocking, actionable warnings for a Zoom source - unlike
    zoom_missing() these don't stop a join, they flag something worth a
    second look. Currently just the registrant-email/bound-account check:
    there's no way to *prove* a tk= token belongs to a given Gmail
    address (Zoom exposes no lookup for it), so this only compares it
    against the account's own verified email and warns on a mismatch."""
    warnings: list[str] = []
    o = d.get("options") or {}
    registrant_email = (o.get("registrant_email") or "").strip().lower()
    if registrant_email:
        from . import accounts as accounts_mod  # local: avoid a module-level cycle (accounts -> control)
        bound_email = (_account_email(d.get("account_id")) or "").strip().lower()
        if bound_email and bound_email != registrant_email:
            warnings.append(
                f"This link was registered with {accounts_mod.mask_email(registrant_email)}, but the account "
                f"bound to this source is {accounts_mod.mask_email(bound_email)} - Zoom may reject the join, or "
                "admit a different registrant than intended."
            )
        elif not d.get("account_id"):
            warnings.append(
                "This is a per-registrant link with a Gmail address on file, but no Google account is bound to "
                "this source - bind the matching account (Accounts page), or join as guest if the webinar allows it."
            )
    return warnings


def set_last_join_method(source_id: int, method: str) -> None:
    """Records which path an auto join actually used (client or web), so
    the UI can show an honest 'Joined via: ...' badge instead of just
    echoing the chosen policy back. No-op for a non-zoom or missing source
    (the join itself already happened; this is just the display record)."""
    if method not in config.ZOOM_JOIN_MODES:
        return
    s = get_source(source_id)
    if not s or s["type"] != "zoom":
        return
    options = dict(s["options"]); options["last_join_method"] = method
    with db.get_conn() as conn:
        conn.execute("UPDATE sources SET options=? WHERE id=?", (json.dumps(options), source_id))
    _bump_rev()


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


def list_sources_public() -> list[dict]:
    return [public_view(s) for s in list_sources()]


# Live sync between /zoom and /remote (and anything else listing sources):
# a monotonic revision bumped by every write here, polled cheaply by the
# pages. Stored in settings so it survives a dashboard restart.
def sources_rev() -> int:
    try:
        return int(db.get_setting("sources_rev", "0") or 0)
    except ValueError:
        return 0


def _bump_rev() -> int:
    rev = sources_rev() + 1
    db.set_setting("sources_rev", str(rev))
    return rev


def reveal_secrets(source_id: int) -> dict:
    """The masked bits of one Zoom source, for an explicit, audited
    'reveal' tap: full URL, passcode, personal join link."""
    s = get_source(source_id)
    if not s or s["type"] != "zoom":
        raise ValidationError("Not a Zoom source")
    o = s.get("options") or {}
    return {"url": s["url"], "passcode": o.get("passcode", ""), "join_url": o.get("join_url", "")}


def mark_joined(source_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE sources SET last_joined_at=? WHERE id=?", (time.time(), source_id))
    _bump_rev()


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
    _bump_rev()


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


ZOOM_VIEWS = {"speaker", "gallery"}
ZOOM_REJOIN_MAX_RANGE = (1, 20)


def _validate_zoom_options(options: dict) -> dict:
    passcode = str(options.get("passcode", "") or "").strip()
    bot_name = str(options.get("bot_name", "Stream Bot") or "Stream Bot").strip()
    signin_mode = str(options.get("signin_mode", "guest") or "guest").strip().lower()
    join_url = str(options.get("join_url", "") or "").strip()
    if not bot_name or len(bot_name) > 64 or any(c in bot_name for c in "\r\n"):
        raise ValidationError("Bot name must be 1-64 characters, no newlines")
    if signin_mode not in config.ZOOM_SIGNIN_MODES:
        raise ValidationError(f"signin_mode must be one of {sorted(config.ZOOM_SIGNIN_MODES)}")
    if passcode and not zoomlink._PWD_RE.match(passcode):
        raise ValidationError("Passcode may only contain letters, digits and . _ - = (max 128)")
    if join_url:
        # The personal (tk=) link saved after completing a registration
        # form by hand - see zoomlink.py.
        try:
            zoomlink.validate_joinable(join_url)
        except zoomlink.ZoomLinkError as exc:
            raise ValidationError(f"Saved join link: {exc}")
        join_url = zoomlink.normalize_url(join_url)
    meeting_kind = str(options.get("meeting_kind", "meeting") or "meeting").strip().lower()
    if meeting_kind not in zoomlink.MEETING_KINDS:
        raise ValidationError(f"meeting_kind must be one of {sorted(zoomlink.MEETING_KINDS)}")
    view = str(options.get("view", "speaker") or "speaker").strip().lower()
    if view not in ZOOM_VIEWS:
        raise ValidationError(f"view must be one of {sorted(ZOOM_VIEWS)}")
    lo, hi = ZOOM_REJOIN_MAX_RANGE
    try:
        rejoin_max = int(options.get("rejoin_max", 5))
    except (TypeError, ValueError):
        raise ValidationError("rejoin_max must be an integer")
    if not (lo <= rejoin_max <= hi):
        raise ValidationError(f"rejoin_max must be between {lo} and {hi}")
    join_at = options.get("join_at")
    if join_at in ("", None):
        join_at = None
    else:
        try:
            join_at = float(join_at)
        except (TypeError, ValueError):
            raise ValidationError("join_at must be a unix timestamp")
        if join_at < time.time() - 60:
            raise ValidationError("The scheduled join time is in the past")
        if join_at > time.time() + 366 * 86400:
            raise ValidationError("The scheduled join time is more than a year away")
    vanity_url = str(options.get("vanity_url", "") or "").strip()
    if vanity_url and zoomlink.classify(vanity_url)["kind"] != "vanity":
        vanity_url = ""
    join_method = str(options.get("join_method", "client") or "client").strip().lower()
    if join_method not in config.ZOOM_JOIN_MODES:
        raise ValidationError(f"join_method must be one of {sorted(config.ZOOM_JOIN_MODES)}")
    registrant_email = str(options.get("registrant_email", "") or "").strip()
    if registrant_email and not _EMAIL_RE.match(registrant_email):
        raise ValidationError("registrant_email doesn't look like an email address")
    # Set by control.set_last_join_method() after a join actually happens
    # (the honest "joined via" record for auto mode) - a save just carries
    # it through unchanged; anything not a real mode is dropped rather
    # than stored, since it only ever feeds a display badge.
    last_join_method = options.get("last_join_method") or ""
    if last_join_method not in config.ZOOM_JOIN_MODES:
        last_join_method = ""
    return {
        "passcode": passcode, "bot_name": bot_name, "signin_mode": signin_mode, "join_url": join_url,
        "meeting_kind": meeting_kind,
        "audio_on": bool(options.get("audio_on", False)),
        "video_on": bool(options.get("video_on", False)),
        "view": view,
        "auto_rejoin": bool(options.get("auto_rejoin", True)),
        "rejoin_max": rejoin_max,
        "join_at": join_at,
        "vanity_url": vanity_url,
        "join_method": join_method,
        "registrant_email": registrant_email,
        "last_join_method": last_join_method,
    }


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
        url = zoomlink.normalize_url(url)
        kind = zoomlink.classify(url)["kind"]
        if kind == "vanity":
            raise ValidationError(
                "Personal room URLs must be resolved to their join link first - paste it on the Zoom page"
            )
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
        sid = cur.lastrowid
    _bump_rev()
    return sid


def _merge_masked(existing: dict | None, type_: str, url: str, options: dict) -> tuple[str, dict]:
    """An edit form only ever sees the public view, so a submitted URL
    equal to the redacted form of the stored one, or a passcode/join_url
    left out (or sent as the mask), means 'keep what is stored'."""
    if not existing or existing["type"] != type_ or type_ != "zoom":
        return url, options
    options = dict(options or {})
    eo = existing.get("options") or {}
    if (url or "").strip() in ("", zoomlink.redact_url(existing["url"])) or "\u2022\u2022\u2022" in (url or ""):
        url = existing["url"]
    for key in ZOOM_SECRET_OPTION_KEYS:
        val = options.get(key)
        if key not in options or val is None or (isinstance(val, str) and ("\u2022" in val)):
            options[key] = eo.get(key, "")
    return url, options


def update_source(source_id: int, name: str, type_: str, url: str, options: dict, account_id=None) -> None:
    name = _validate_name(name)
    url, options = _merge_masked(get_source(source_id), type_, url, options)
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
    _bump_rev()


def duplicate_source(source_id: int) -> int:
    """A copy named '<name> (copy)' (numbered if taken), secrets included -
    this never leaves the process, it goes straight back into the table."""
    s = get_source(source_id)
    if not s:
        raise ValidationError("Source not found")
    existing = {x["name"] for x in list_sources()}
    base = f"{s['name']} (copy)"
    name, n = base, 2
    while name in existing:
        name = f"{base} {n}"; n += 1
    name = name[:64]
    now = time.time()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sources (name, type, url, options, created_at, updated_at, account_id) VALUES (?,?,?,?,?,?,?)",
            (name, s["type"], s["url"], json.dumps(s["options"]), now, now, s.get("account_id")),
        )
        sid = cur.lastrowid
    _bump_rev()
    return sid


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
    options = dict(s["options"]); options["join_url"] = zoomlink.normalize_url(join_url)
    with db.get_conn() as conn:
        conn.execute("UPDATE sources SET options=?, updated_at=? WHERE id=?",
                     (json.dumps(options), time.time(), source_id))
    _bump_rev()
    return get_source(source_id)


def set_account(source_id: int, account_id) -> None:
    account_id = _validate_account_id(account_id)
    with db.get_conn() as conn:
        conn.execute("UPDATE sources SET account_id=?, updated_at=? WHERE id=?", (account_id, time.time(), source_id))
    _bump_rev()


def delete_source(source_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    if db.get_setting("active_source_id") == str(source_id):
        set_active_source_id(None)
    _bump_rev()
