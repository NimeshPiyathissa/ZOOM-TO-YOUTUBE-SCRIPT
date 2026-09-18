"""Zoom link classification (Part 3B).

Three very different things all look like "a Zoom link":

  meeting       https://<sub>.zoom.us/j/<id>?pwd=...
                A normal meeting/webinar join link. The client can join
                it directly (zoommtg://zoom.us/join?confno=<id>&pwd=...).

  personal      https://<sub>.zoom.us/w/<id>?tk=<token>&pwd=...
                (also /j/<id>?tk=...)
                The per-registrant join link Zoom issues AFTER someone
                registers for a webinar (or a meeting with registration).
                The tk= token identifies that one registrant: it is
                single-use per registrant, must not be shared, and can
                expire or be invalidated by the host. This is the only
                kind of link the client can use to join a
                registration-required webinar.

  registration  https://<sub>.zoom.us/webinar/register/<id>
                https://<sub>.zoom.us/meeting/register/<id>
                A web FORM, not a join link. There is nothing for the
                Zoom client to join here - a human fills in name/email,
                and Zoom then issues a `personal` link (shown on the
                confirmation page and/or emailed). The dashboard's flow:
                open this page over noVNC, complete it by hand, paste
                the resulting personal link back and save it against the
                source. No form automation is ever attempted.

tk= tokens are credentials in practice (they let anyone join as that
registrant), so they live in .env's ZOOM_LINK like pwd= does, and
logs.py redacts them."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_HOST_RE = re.compile(r"^([a-z0-9-]+\.)*zoom\.us$", re.IGNORECASE)
_JOIN_PATH_RE = re.compile(r"^/(j|w)/(\d{9,11})/?$")
_REG_PATH_RE = re.compile(r"^/(webinar|meeting)/register/([A-Za-z0-9_-]{6,120})/?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.=-]{8,400}$")


class ZoomLinkError(Exception):
    pass


def classify(url: str) -> dict:
    """Returns {kind, meeting_id, has_pwd, has_tk, registration_id, host}.
    kind is one of meeting / personal / registration / unknown."""
    url = (url or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    out = {"kind": "unknown", "meeting_id": None, "has_pwd": False, "has_tk": False,
           "registration_id": None, "host": host}
    if parts.scheme.lower() != "https" or not _HOST_RE.match(host):
        return out
    qs = parse_qs(parts.query)
    tk = (qs.get("tk") or [None])[0]
    pwd = (qs.get("pwd") or [None])[0]
    m = _JOIN_PATH_RE.match(parts.path)
    if m:
        out["meeting_id"] = m.group(2)
        out["has_pwd"] = bool(pwd)
        out["has_tk"] = bool(tk)
        out["kind"] = "personal" if tk else "meeting"
        return out
    r = _REG_PATH_RE.match(parts.path)
    if r:
        out["kind"] = "registration"
        out["registration_id"] = r.group(2)
        return out
    return out


def validate_joinable(url: str) -> dict:
    """A link the Zoom client can actually join (meeting or personal).
    Raises ZoomLinkError otherwise, with a reason a human can act on."""
    info = classify(url)
    if info["kind"] == "registration":
        raise ZoomLinkError(
            "That is a registration page, not a join link. Complete the registration "
            "(Open registration below), then paste the personal join link Zoom gives you."
        )
    if info["kind"] not in ("meeting", "personal"):
        raise ZoomLinkError("Not a recognized Zoom join link (expected https://<x>.zoom.us/j/<id> or /w/<id>?tk=...)")
    if info["has_tk"]:
        tk = (parse_qs(urlsplit(url).query).get("tk") or [""])[0]
        if not _TOKEN_RE.match(tk):
            raise ZoomLinkError("The tk= token in that link doesn't look valid")
    return info


def validate_registration(url: str) -> dict:
    info = classify(url)
    if info["kind"] != "registration":
        raise ZoomLinkError("Not a Zoom registration page (expected https://<x>.zoom.us/webinar/register/...)")
    return info
