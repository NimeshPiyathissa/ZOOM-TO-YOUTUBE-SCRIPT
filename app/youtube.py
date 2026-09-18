"""Turns a pasted YouTube URL (watch/playlist/youtu.be/embed/shorts) into
a clean embed URL for Chrome's kiosk window to navigate to - autoplay,
unmuted, no surrounding YouTube UI. Video/playlist IDs are validated
against YouTube's own ID character set before being placed in the
constructed URL, and the original URL is always run through
url_security.validate_url() first (http(s)-only, private-IP/metadata
blocklist), exactly like every other source URL in this app."""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs

from . import url_security

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")


class YouTubeURLError(Exception):
    pass


def _valid_id(value: str | None) -> str | None:
    if value and _ID_RE.match(value):
        return value
    return None


def to_embed_url(raw_url: str) -> str:
    """Raises YouTubeURLError or url_security.URLSecurityError."""
    url_security.validate_url(raw_url, "webpage")
    parsed = urlparse(raw_url.strip())
    host = parsed.netloc.lower()
    if host not in _YT_HOSTS:
        raise YouTubeURLError(f"'{host}' is not a recognized YouTube host")

    qs = parse_qs(parsed.query)
    playlist_id = _valid_id((qs.get("list") or [None])[0])
    video_id = None
    if host == "youtu.be":
        video_id = _valid_id(parsed.path.lstrip("/"))
    elif parsed.path.startswith("/watch"):
        video_id = _valid_id((qs.get("v") or [None])[0])
    elif parsed.path.startswith("/embed/"):
        video_id = _valid_id(parsed.path.split("/embed/", 1)[1].split("/")[0])
    elif parsed.path.startswith("/shorts/"):
        video_id = _valid_id(parsed.path.split("/shorts/", 1)[1].split("/")[0])

    params = ["autoplay=1", "mute=0", "playsinline=1", "rel=0"]
    if playlist_id:
        params.append(f"list={playlist_id}")

    if video_id:
        base = f"https://www.youtube.com/embed/{video_id}"
    elif playlist_id:
        base = "https://www.youtube.com/embed/videoseries"
    else:
        raise YouTubeURLError("Could not find a video or playlist ID in that URL")
    return base + "?" + "&".join(params)
