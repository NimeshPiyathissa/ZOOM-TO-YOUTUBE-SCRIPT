"""Zoom link classification (Part 3B) and smart-paste parsing (Zoom page).

Three very different things all look like "a Zoom link":

  meeting       https://<sub>.zoom.us/j/<id>?pwd=...
                A normal meeting/webinar join link. It can be joined via
                the desktop client (zoommtg://zoom.us/join?confno=<id>&pwd=...,
                see build_join_url/build_wc_join_url) or via Zoom's own web
                client (https://<sub>.zoom.us/wc/join/<id>?pwd=...) - both
                are the same meeting, just a different join path. /wc/join/
                is recognized as the exact same `kind` as /j/ (the *shape*
                of the link doesn't imply which path an operator wants -
                that's a separate per-meeting choice, see the dashboard's
                join_method option).

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

parse_any() (the Zoom page's single smart-paste box) additionally
understands what people actually paste: a bare meeting ID, a personal
meeting room / vanity URL (zoom.us/my/<name>, resolved through Zoom's own
redirect to the /j/ link it stands for), a zoommtg:// deep link copied
out of an invite, and a whole email invite blob ("Meeting ID: ...",
"Passcode: ..."). Everything it returns is normalized to the https
join-link form the rest of the pipeline already handles; the passcode
travels separately (options.passcode) unless the link itself carried
pwd=.

tk= tokens and pwd= are credentials in practice, so they live in .env's
ZOOM_LINK like pwd= does, logs.py redacts them, and redact_url() below is
what every UI/audit view of a link goes through."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit, quote

_HOST_RE = re.compile(r"^([a-z0-9-]+\.)*zoom\.us$", re.IGNORECASE)
_JOIN_PATH_RE = re.compile(r"^/(?:j|w|wc/join)/(\d{9,11})/?$")
_REG_PATH_RE = re.compile(r"^/(webinar|meeting)/register/([A-Za-z0-9_-]{6,120})/?$")
_VANITY_PATH_RE = re.compile(r"^/my/([A-Za-z0-9._-]{3,64})/?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.=-]{8,400}$")
_PWD_RE = re.compile(r"^[A-Za-z0-9_.=-]{1,128}$")
_SECRET_QUERY_RE = re.compile(r"((?:pwd|tk)=)[^&#\s]+", re.IGNORECASE)

# Query keys Zoom appends for its own analytics; dropped when normalizing
# so two pastes of the same link compare equal and nothing noisy is saved.
_NOISE_QUERY_KEYS = {"_x_zm_rtaid", "_x_zm_rhtaid", "from", "utm_source", "utm_medium", "utm_campaign"}

MEETING_KINDS = {"meeting", "webinar", "pmi"}


class ZoomLinkError(Exception):
    pass


# ---------------------------------------------------------------- classification (unchanged contract)

def classify(url: str) -> dict:
    """Returns {kind, meeting_id, has_pwd, has_tk, registration_id, host}.
    kind is one of meeting / personal / registration / vanity / unknown."""
    url = (url or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    out = {"kind": "unknown", "meeting_id": None, "has_pwd": False, "has_tk": False,
           "registration_id": None, "host": host, "vanity": None}
    if parts.scheme.lower() != "https" or not _HOST_RE.match(host):
        return out
    qs = parse_qs(parts.query)
    tk = (qs.get("tk") or [None])[0]
    pwd = (qs.get("pwd") or [None])[0]
    m = _JOIN_PATH_RE.match(parts.path)
    if m:
        out["meeting_id"] = m.group(1)
        out["has_pwd"] = bool(pwd)
        out["has_tk"] = bool(tk)
        out["kind"] = "personal" if tk else "meeting"
        return out
    r = _REG_PATH_RE.match(parts.path)
    if r:
        out["kind"] = "registration"
        out["registration_id"] = r.group(2)
        return out
    v = _VANITY_PATH_RE.match(parts.path)
    if v:
        out["kind"] = "vanity"
        out["vanity"] = v.group(1)
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
    if info["kind"] == "vanity":
        raise ZoomLinkError("That is a personal meeting room URL - use the Zoom page's paste box, which resolves it to its join link")
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


# ---------------------------------------------------------------- redaction / normalization

def redact_url(url: str) -> str:
    """pwd= and tk= values replaced with a fixed mask - the form every
    list view, audit entry and log line uses. Idempotent."""
    return _SECRET_QUERY_RE.sub(r"\g<1>•••", url or "")


def format_meeting_id(meeting_id: str | None) -> str:
    """'1234567890' -> '123 456 7890' (10-11 digits), '123 456 789' for 9."""
    d = re.sub(r"\D", "", meeting_id or "")
    if len(d) == 11:
        return f"{d[:3]} {d[3:7]} {d[7:]}"
    if len(d) == 10:
        return f"{d[:3]} {d[3:6]} {d[6:]}"
    if len(d) == 9:
        return f"{d[:3]} {d[3:6]} {d[6:]}"
    return d


def normalize_url(url: str) -> str:
    """Canonical https join/registration link: fragment (#success etc.)
    dropped, analytics query keys dropped, host lower-cased, path
    trailing slash removed. Secrets (pwd/tk) are kept verbatim."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    keep = []
    for kv in parts.query.split("&"):
        if not kv:
            continue
        k = kv.split("=", 1)[0]
        if k.lower() in _NOISE_QUERY_KEYS:
            continue
        keep.append(kv)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "&".join(keep), ""))


def build_join_url(meeting_id: str, passcode: str | None = None, tk: str | None = None, host: str = "zoom.us") -> str:
    """The https /j/ link for an ID typed by hand or pulled from a
    zoommtg:// deep link. pwd= is included only when given, so a
    passcode entered separately stays in options.passcode (masked)."""
    q = []
    if passcode:
        q.append("pwd=" + quote(passcode, safe=""))
    if tk:
        q.append("tk=" + quote(tk, safe=""))
    return f"https://{host}/j/{meeting_id}" + ("?" + "&".join(q) if q else "")


def build_wc_join_url(meeting_id: str, passcode: str | None = None, tk: str | None = None, host: str = "zoom.us") -> str:
    """The https /wc/join/ link Zoom's own web client uses - same meeting,
    same secrets, different join path. See control.py's join_method=web."""
    q = []
    if passcode:
        q.append("pwd=" + quote(passcode, safe=""))
    if tk:
        q.append("tk=" + quote(tk, safe=""))
    return f"https://{host}/wc/join/{meeting_id}" + ("?" + "&".join(q) if q else "")


def extract_credentials(url: str) -> dict:
    """meeting_id/pwd/tk/host pulled out of any recognized join link
    (/j/, /w/, /wc/join/), so a caller that already holds the secrets
    (control.py) can rebuild the *other* shape of link (deep link vs
    wc/join) without re-deriving the parsing logic. Not for anything
    that leaves the process unmasked - this returns raw secrets."""
    info = classify(url)
    qs = parse_qs(urlsplit(url).query)
    return {
        "meeting_id": info["meeting_id"],
        "pwd": (qs.get("pwd") or [None])[0],
        "tk": (qs.get("tk") or [None])[0],
        "host": info["host"] or "zoom.us",
    }


# ---------------------------------------------------------------- smart paste

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_ZOOMMTG_RE = re.compile(r"zoommtg://[^\s<>\"')\]]+", re.IGNORECASE)
_ID_LABEL_RE = re.compile(r"(?:meeting|webinar)\s*id\s*[:：]\s*([\d][\d \- ]{7,16}\d)", re.IGNORECASE)
_PASSCODE_LABEL_RE = re.compile(r"(?:passcode|password|pass\s*code)\s*[:：]\s*([^\s\r\n]{1,64})", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^\s*(\d[\d \-]{7,16}\d)\s*$")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _unwrap_redirect(url: str) -> str | None:
    """A URL whose host isn't zoom.us may be a redirect wrapper (Outlook
    Safe Links, corporate mail-gateway proxies, generic link-tracking
    services) carrying the real zoom.us URL in a query parameter.
    parse_qs already URL-decodes each value, so a plain
    ?url=https%3A%2F%2Fus06web.zoom.us%2Fj%2F... shape is found directly.
    A vendor with its own non-standard encoding (e.g. Proofpoint URL
    Defense's character-substituted URLs) isn't handled here - add a
    decoder for that specific shape if a real sample ever shows up.
    Returns the embedded zoom.us URL, or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    for values in parse_qs(parts.query).values():
        for v in values:
            if _HOST_RE.match((urlsplit(v).hostname or "").lower()):
                return v
    return None


def _empty() -> dict:
    return {
        "ok": False, "input_kind": None, "meeting_kind": None, "meeting_id": None,
        "meeting_id_formatted": "", "passcode": None, "has_passcode": False, "has_tk": False,
        "url": None, "url_redacted": None, "registration_url": None, "vanity": None,
        "bot_display_name": None, "warnings": [], "errors": [], "needs_resolve": False,
    }


def _finish(out: dict) -> dict:
    if out.get("url"):
        out["url_redacted"] = redact_url(out["url"])
    out["meeting_id_formatted"] = format_meeting_id(out.get("meeting_id"))
    out["has_passcode"] = bool(out.get("passcode")) or bool(out.get("url") and classify(out["url"])["has_pwd"])
    if out.get("meeting_kind") is None and out.get("meeting_id"):
        out["meeting_kind"] = "meeting"
    out["ok"] = not out["errors"] and (bool(out.get("meeting_id")) or bool(out.get("registration_url")) or out["needs_resolve"])
    return out


def _from_https(url: str, out: dict, text_passcode: str | None = None) -> dict:
    url = normalize_url(url)
    info = classify(url)
    qs = parse_qs(urlsplit(url).query)
    if info["kind"] in ("meeting", "personal"):
        out["input_kind"] = "personal_link" if info["has_tk"] else "join_link"
        out["meeting_id"] = info["meeting_id"]
        out["url"] = url
        out["has_tk"] = info["has_tk"]
        if info["has_tk"]:
            tk = (qs.get("tk") or [""])[0]
            if not _TOKEN_RE.match(tk):
                out["errors"].append("The tk= registrant token in this link doesn't look valid.")
            out["meeting_kind"] = "webinar"
            out["warnings"].append(
                "Personal registrant link: it is tied to the one person who registered and can expire or be "
                "revoked by the host. Don't share it; if the join fails later, re-register for a fresh one."
            )
        if "/w/" in urlsplit(url).path:
            out["meeting_kind"] = "webinar"
        pwd = (qs.get("pwd") or [None])[0]
        if pwd and not _PWD_RE.match(pwd):
            out["errors"].append("The pwd= value in this link contains characters Zoom never uses.")
        if not pwd and text_passcode:
            out["passcode"] = text_passcode
        elif not pwd:
            out["warnings"].append("No passcode in the link. If the meeting needs one, add it below.")
        return out
    if info["kind"] == "registration":
        out["input_kind"] = "registration"
        out["meeting_kind"] = "webinar" if "/webinar/" in urlsplit(url).path else "meeting"
        out["registration_url"] = url
        out["url"] = url
        out["warnings"].append(
            "This is a registration form, not a join link. Open it on the remote screen, register, then save the "
            "personal join link Zoom issues (shown on the confirmation page and emailed)."
        )
        return out
    if info["kind"] == "vanity":
        out["input_kind"] = "vanity"
        out["meeting_kind"] = "pmi"
        out["vanity"] = info["vanity"]
        out["url"] = url
        out["needs_resolve"] = True
        if text_passcode:
            out["passcode"] = text_passcode
        return out
    out["errors"].append("That zoom.us URL isn't a join link, a registration page or a personal room (/j/, /w/, /webinar/register/, /my/).")
    return out


def _from_zoommtg(link: str, out: dict) -> dict:
    """zoommtg://zoom.us/join?action=join&confno=<id>&pwd=<x>&tk=<t>&uname=<n>"""
    parts = urlsplit(link)
    qs = parse_qs(parts.query)
    confno = _digits((qs.get("confno") or [""])[0])
    out["input_kind"] = "deep_link"
    if not (9 <= len(confno) <= 11):
        out["errors"].append("The zoommtg:// link has no valid confno= meeting ID.")
        return out
    pwd = (qs.get("pwd") or [None])[0]
    tk = (qs.get("tk") or [None])[0]
    uname = (qs.get("uname") or [None])[0]
    if pwd and not _PWD_RE.match(pwd):
        out["errors"].append("The pwd= value in the deep link contains unexpected characters.")
    if tk and not _TOKEN_RE.match(tk):
        out["errors"].append("The tk= token in the deep link doesn't look valid.")
    out["meeting_id"] = confno
    out["url"] = build_join_url(confno, pwd, tk)
    out["has_tk"] = bool(tk)
    if tk:
        out["meeting_kind"] = "webinar"
        out["warnings"].append("Carries a registrant token (tk=) - tied to one registrant, may expire.")
    if uname:
        out["bot_display_name"] = uname[:64]
    if not pwd:
        out["warnings"].append("No passcode in the deep link. If the meeting needs one, add it below.")
    return out


def parse_any(text: str, passcode: str | None = None) -> dict:
    """Smart paste. `text` is whatever landed in the box; `passcode` is the
    optional separate field (used when the link/text carries none).
    Never raises - problems come back in `errors`, hints in `warnings`,
    so the UI can show what was understood and let the operator fix it."""
    out = _empty()
    text = (text or "").strip()
    passcode = (passcode or "").strip() or None
    if passcode and not _PWD_RE.match(passcode):
        out["errors"].append("Passcode contains characters Zoom never uses (letters, digits, . _ - = only).")
        passcode = None
    if not text:
        out["errors"].append("Paste a Zoom link, meeting ID or invite text.")
        return _finish(out)

    # 1. a deep link anywhere in the text wins (it's the most specific)
    m = _ZOOMMTG_RE.search(text)
    if m:
        res = _from_zoommtg(m.group(0), out)
        if passcode and not res.get("passcode") and not classify(res.get("url") or "")["has_pwd"]:
            res["passcode"] = passcode
            res["warnings"] = [w for w in res["warnings"] if not w.startswith("No passcode")]
        return _finish(res)

    # 2. any zoom.us https URL in the text (a bare link, or inside an invite)
    text_pass = None
    pm = _PASSCODE_LABEL_RE.search(text)
    if pm and _PWD_RE.match(pm.group(1)):
        text_pass = pm.group(1)
    zoom_urls = []
    for um in _URL_IN_TEXT_RE.finditer(text):
        raw_cand = um.group(0).rstrip(".,;:")
        cand = raw_cand
        if not _HOST_RE.match((urlsplit(cand).hostname or "").lower()):
            unwrapped = _unwrap_redirect(cand)
            if unwrapped:
                cand = unwrapped
        if _HOST_RE.match((urlsplit(cand).hostname or "").lower()):
            zoom_urls.append(cand)
            info = classify(normalize_url(cand))
            if info["kind"] != "unknown":
                # Compared against the matched substring as it appeared in
                # the input (before unwrapping a redirect wrapper) - a
                # wrapped link that's the *whole* paste shouldn't be
                # mistaken for "a link inside a longer invite blob" just
                # because the wrapper itself is longer than the meeting
                # link it carries.
                is_invite = len(text) > len(raw_cand) + 20
                res = _from_https(cand, out, text_pass or passcode)
                if is_invite:
                    res["input_kind"] = "invite"
                if res.get("meeting_kind") is None and re.search(r"\bwebinar\b", text, re.I):
                    res["meeting_kind"] = "webinar"
                if passcode and not res.get("passcode") and not classify(res["url"] or "")["has_pwd"]:
                    res["passcode"] = passcode
                return _finish(res)

    # 3. "Meeting ID: 123 4567 8901" in invite text, or a bare ID
    im = _ID_LABEL_RE.search(text) or _BARE_ID_RE.match(text)
    if im:
        mid = _digits(im.group(1))
        if 9 <= len(mid) <= 11:
            out["input_kind"] = "invite" if _ID_LABEL_RE.search(text) else "meeting_id"
            out["meeting_id"] = mid
            out["passcode"] = text_pass or passcode
            out["url"] = build_join_url(mid)
            if re.search(r"\bwebinar\b", text, re.I):
                out["meeting_kind"] = "webinar"
            if not out["passcode"]:
                out["warnings"].append("No passcode found. Most meetings need one - add it below if so.")
            return _finish(out)
        out["errors"].append(f"A Zoom meeting ID has 9-11 digits; got {len(mid)}.")
        return _finish(out)

    if zoom_urls:
        return _finish(_from_https(zoom_urls[0], out, text_pass or passcode))   # a zoom.us URL of an unknown shape: its own error
    if _URL_IN_TEXT_RE.search(text):
        out["errors"].append("That link isn't on zoom.us. Only Zoom links can be joined here.")
    else:
        out["errors"].append("Couldn't find a Zoom link, a meeting ID or an invite in that text.")
    return _finish(out)


def resolve_vanity(url: str, timeout: float = 8.0) -> dict:
    """zoom.us/my/<name> -> the /j/<id>[?pwd=] link Zoom redirects it to.
    One request, redirects NOT followed automatically: only a Location
    on a zoom.us host that classifies as a join link is accepted, and at
    most 3 hops. Raises ZoomLinkError with the reason otherwise."""
    import httpx
    info = classify(url)
    if info["kind"] != "vanity":
        raise ZoomLinkError("Not a personal meeting room URL")
    current = normalize_url(url)
    hops = 0
    with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}) as client:
        while hops < 3:
            resp = client.get(current)
            loc = resp.headers.get("location")
            if resp.status_code in (301, 302, 303, 307, 308) and loc:
                loc = httpx.URL(current).join(loc).__str__()
                host = (urlsplit(loc).hostname or "").lower()
                if not _HOST_RE.match(host):
                    raise ZoomLinkError("The room redirected off zoom.us - refusing to follow it")
                nxt = normalize_url(loc)
                k = classify(nxt)
                if k["kind"] in ("meeting", "personal"):
                    return {"url": nxt, "meeting_id": k["meeting_id"], "has_pwd": k["has_pwd"]}
                current = nxt
                hops += 1
                continue
            break
    raise ZoomLinkError(
        "Zoom didn't redirect that room to a join link (it may not exist, or it needs a browser). "
        "Enter the meeting ID and passcode from the invite instead."
    )
