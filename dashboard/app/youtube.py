"""YouTube links: smart-paste parsing, metadata lookup and the watch-page
URL Chrome's kiosk navigates to (never /embed/ - see to_play_url).

parse_any() (the Remote page's YouTube paste box) understands what
people actually paste: watch?v=, youtu.be/<id>, /shorts/, /live/<id>,
/embed/, music.youtube.com, a playlist (list=), a video inside a
playlist, a channel's /live page (@handle, /channel/UC..., /c/, /user/),
a bare 11-character video id, timestamps (t=1h2m3s, t=90, start=,
#t=), and share text with a link buried in it. share params (si=,
feature=) are dropped. Everything comes back normalized to a canonical
URL plus a `kind` (video / playlist / live_channel) and a start offset.

lookup() asks YouTube's public oEmbed endpoint for the title, channel
and thumbnail - no API key, no account. resolve_live() turns a channel's
/live page into the video id that is live right now (from the page's
ytInitialPlayerResponse), or says the channel is offline.

Video/playlist IDs are validated against YouTube's own ID character set
before being placed in a constructed URL, and every original URL is run
through url_security.validate_url() first (http(s)-only, private-IP/
metadata blocklist), exactly like every other source URL in this app."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse, parse_qs, quote

from . import url_security

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_TIME_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"

KINDS = {"video", "playlist", "live_channel"}
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)


class YouTubeURLError(Exception):
    pass


def _valid_id(value: str | None) -> str | None:
    if value and _ID_RE.match(value):
        return value
    return None


def parse_time(value: str | None) -> int | None:
    """'90' / '90s' / '1h2m3s' / '2m' -> seconds. None when absent or odd."""
    if not value:
        return None
    v = value.strip().lower()
    if v.isdigit():
        return int(v)
    m = _TIME_RE.match(v)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def format_time(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- classification

def classify(url: str) -> dict:
    """{kind, video_id, list_id, channel, start, canonical, host} - kind is
    video / playlist / live_channel / channel / unknown."""
    url = (url or "").strip()
    out = {"kind": "unknown", "video_id": None, "list_id": None, "channel": None, "start": None, "canonical": None, "host": ""}
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return out
    host = (p.hostname or "").lower()
    out["host"] = host
    if host not in _YT_HOSTS:
        return out
    qs = parse_qs(p.query)
    path = p.path or "/"
    start = parse_time((qs.get("t") or qs.get("start") or [None])[0])
    if start is None and p.fragment.startswith("t="):
        start = parse_time(p.fragment[2:])
    list_id = _valid_id((qs.get("list") or [None])[0])
    vid = None
    if host == "youtu.be":
        vid = path.lstrip("/").split("/")[0]
    elif path.startswith("/watch"):
        vid = (qs.get("v") or [None])[0]
    else:
        for prefix in ("/embed/", "/shorts/", "/live/", "/v/"):
            if path.startswith(prefix):
                vid = path[len(prefix):].split("/")[0]
                break
    if vid and _VIDEO_ID_RE.match(vid):
        out.update({"kind": "video", "video_id": vid, "list_id": list_id, "start": start})
        canon = f"https://www.youtube.com/watch?v={vid}"
        if list_id:
            canon += f"&list={list_id}"
        if start:
            canon += f"&t={start}"
        out["canonical"] = canon
        return out
    if path.startswith("/playlist") and list_id:
        out.update({"kind": "playlist", "list_id": list_id, "canonical": f"https://www.youtube.com/playlist?list={list_id}"})
        return out
    if path.startswith("/embed/videoseries") and list_id:
        out.update({"kind": "playlist", "list_id": list_id, "canonical": f"https://www.youtube.com/playlist?list={list_id}"})
        return out
    # channel forms: /@handle[/live], /channel/UC...[/live], /c/name[/live], /user/name[/live]
    segs = [s for s in path.split("/") if s]
    chan = None
    if segs and _HANDLE_RE.match(segs[0]):
        chan = segs[0]
    elif len(segs) >= 2 and segs[0] in ("channel", "c", "user"):
        if segs[0] == "channel" and not _CHANNEL_ID_RE.match(segs[1]):
            return out
        if segs[0] != "channel" and not re.match(r"^[A-Za-z0-9._-]{1,60}$", segs[1]):
            return out
        chan = f"{segs[0]}/{segs[1]}"
    if chan:
        is_live = (len(segs) >= 2 and segs[-1] == "live")
        out["channel"] = chan
        out["kind"] = "live_channel" if is_live else "channel"
        out["canonical"] = f"https://www.youtube.com/{chan}/live" if is_live else f"https://www.youtube.com/{chan}"
        return out
    if list_id:
        out.update({"kind": "playlist", "list_id": list_id, "canonical": f"https://www.youtube.com/playlist?list={list_id}"})
    return out


# ---------------------------------------------------------------- smart paste

def _empty() -> dict:
    return {"ok": False, "input_kind": None, "kind": None, "video_id": None, "list_id": None, "channel": None,
            "start": None, "start_formatted": "", "url": None, "thumbnail_url": None, "title": None, "author": None,
            "warnings": [], "errors": []}


def parse_any(text: str) -> dict:
    """Never raises: errors/warnings come back for the operator."""
    out = _empty()
    text = (text or "").strip()
    if not text:
        out["errors"].append("Paste a YouTube link, playlist, channel /live page, or a video ID.")
        return out
    urls = [u.rstrip(".,;:)") for u in _URL_IN_TEXT_RE.findall(text)]
    cand = None
    for u in urls:
        if (urlparse(u).hostname or "").lower() in _YT_HOSTS:
            cand = u
            break
    if cand is None and not urls and _VIDEO_ID_RE.match(text):
        cand = f"https://www.youtube.com/watch?v={text}"
        out["input_kind"] = "video_id"
    elif cand is None and not urls and re.match(r"^[A-Za-z0-9._-]+\.(com|be)/", text):
        cand = "https://" + text
    if cand is None:
        out["errors"].append("That isn't a YouTube link." if urls else "Couldn't find a YouTube link or video ID in that text.")
        return out
    info = classify(cand)
    if out["input_kind"] is None:
        out["input_kind"] = "share_text" if len(text) > len(cand) + 12 else {"video": "video_link", "playlist": "playlist_link", "live_channel": "live_link", "channel": "channel_link"}.get(info["kind"], "link")
    if info["kind"] == "channel":
        out["errors"].append("That is a channel page, which has nothing to play. Use the channel's /live link (for its live stream) or a video or playlist from it.")
        out["kind"] = "channel"; out["channel"] = info["channel"]
        return out
    if info["kind"] == "unknown":
        out["errors"].append("Couldn't find a video, playlist or live channel in that YouTube link.")
        return out
    out.update({"kind": info["kind"], "video_id": info["video_id"], "list_id": info["list_id"], "channel": info["channel"],
                "start": info["start"], "start_formatted": format_time(info["start"]), "url": info["canonical"]})
    if info["video_id"]:
        out["thumbnail_url"] = f"https://i.ytimg.com/vi/{info['video_id']}/hqdefault.jpg"
    if info["kind"] == "video" and info["list_id"]:
        out["warnings"].append("Video inside a playlist: playback continues with the playlist after it.")
    if info["kind"] == "live_channel":
        out["warnings"].append("A channel's live page: whatever the channel is streaming when you press play. If it isn't live, playback can't start.")
    if info["host"].startswith("music."):
        out["warnings"].append("YouTube Music link - it plays as the regular YouTube video.")
    if "/shorts/" in cand:
        out["warnings"].append("A Short - it plays as a normal video (vertical, letterboxed on the stream).")
    out["ok"] = True
    return out


# ---------------------------------------------------------------- metadata (oEmbed, no key) and live resolution

def lookup(url: str, timeout: float = 8.0) -> dict:
    """Title / channel / thumbnail from YouTube's public oEmbed endpoint.
    Works for videos and playlists; anything else (or a private video)
    comes back {} rather than raising - metadata is decoration."""
    import httpx
    info = classify(url)
    if info["kind"] not in ("video", "playlist"):
        return {}
    target = info["canonical"]
    try:
        r = httpx.get("https://www.youtube.com/oembed", params={"url": target, "format": "json"},
                      timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=False)
        if r.status_code != 200:
            return {"unavailable": True, "status": r.status_code}
        d = r.json()
    except Exception:  # noqa: BLE001 - network problems are not the operator's problem
        return {}
    thumb = d.get("thumbnail_url") or ""
    if not re.match(r"^https://i\.ytimg\.com/", thumb):
        thumb = f"https://i.ytimg.com/vi/{info['video_id']}/hqdefault.jpg" if info["video_id"] else None
    return {"title": (d.get("title") or "")[:200], "author": (d.get("author_name") or "")[:120], "thumbnail_url": thumb}


def resolve_live(url: str, timeout: float = 12.0) -> dict:
    """A channel's /live page -> {video_id, live: bool}. Reads the id from
    the page's ytInitialPlayerResponse; only youtube.com is contacted."""
    import httpx
    info = classify(url)
    if info["kind"] != "live_channel":
        raise YouTubeURLError("Not a channel /live link")
    try:
        r = httpx.get(info["canonical"], timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.8"})
    except Exception as exc:  # noqa: BLE001
        raise YouTubeURLError("Couldn't reach youtube.com to see whether the channel is live") from exc
    html = r.text
    i = html.find("ytInitialPlayerResponse")
    seg = html[i:i + 200000] if i >= 0 else ""
    m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', seg)
    live = bool(re.search(r'"isLiveNow":true|"isLive":true', seg))
    if not m:
        return {"video_id": None, "live": False}
    return {"video_id": m.group(1), "live": live}


# ---------------------------------------------------------------- the URL Chrome navigates to

def to_play_url(raw_url: str, options: dict | None = None) -> str:
    """The watch-page URL the kiosk navigates to. NOT an /embed/ URL: a
    top-level navigation to /embed/ has no referring page, and YouTube
    now refuses that with "Video player configuration error (153)". The
    watch page autoplays under the kiosk's autoplay policy; the dashboard
    then fullscreens the player and applies loop / captions / speed over
    DevTools (those can't ride on the URL). Raises YouTubeURLError or
    url_security.URLSecurityError."""
    url_security.validate_url(raw_url, "webpage")
    info = classify(raw_url)
    o = options or {}
    start = o.get("start") if o.get("start") not in (None, "", 0) else info.get("start")
    if info["kind"] == "video":
        url = f"https://www.youtube.com/watch?v={info['video_id']}"
        if info["list_id"]:
            url += f"&list={info['list_id']}"
    elif info["kind"] == "playlist":
        # watch?list=<id> lands on the playlist's first video with the
        # playlist attached, so YouTube itself plays on through it.
        url = f"https://www.youtube.com/watch?list={info['list_id']}"
    elif info["kind"] == "live_channel":
        r = resolve_live(raw_url)
        if not r["video_id"]:
            raise YouTubeURLError("That channel isn't live right now - nothing to play")
        url = f"https://www.youtube.com/watch?v={r['video_id']}"
    else:
        raise YouTubeURLError("Could not find a video, playlist or live channel in that URL")
    if start and info["kind"] == "video":
        url += f"&t={int(start)}s"
    return url + "&autoplay=1"


# Kept for callers/tests that still use the old name.
to_embed_url = to_play_url


def video_id_from_url(raw_url: str) -> str | None:
    return classify(raw_url).get("video_id")


def thumbnail_url(raw_url: str) -> str | None:
    """YouTube's public thumbnail CDN - no API key. Playlists have no
    single thumbnail; the UI shows a generic tile for those."""
    vid = video_id_from_url(raw_url)
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None


def validate_options(options: dict | None) -> dict:
    o = options or {}
    start = o.get("start")
    if start in (None, ""):
        start = None
    else:
        try:
            start = int(start)
        except (TypeError, ValueError):
            start = parse_time(str(start))
        if start is None or start < 0 or start > 24 * 3600:
            raise YouTubeURLError("Start time must be 0-24h (e.g. 90 or 1m30s)")
    speed = o.get("speed", 1.0)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        raise YouTubeURLError("speed must be a number")
    if speed not in SPEEDS:
        raise YouTubeURLError(f"speed must be one of {', '.join(str(s) for s in SPEEDS)}")
    return {"start": start, "loop": bool(o.get("loop", False)), "captions": bool(o.get("captions", False)), "speed": speed}
