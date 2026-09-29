"""ffprobe wrapper for direct-media sources (Change 1). Runs as the
`dashboard` user directly - a read-only probe of a (validated) URL needs
no zoombot/X11/pulse privilege, so it deliberately doesn't go through the
sudo plumbing in control.py; keeping it out of the zoombot trust boundary
is the more conservative choice, not less."""
from __future__ import annotations

import asyncio
import json

from . import config


class ProbeError(Exception):
    pass


async def probe_url(url: str) -> dict:
    """Returns a small summary dict: video/audio codec, resolution, fps,
    and whether the codecs look safe to copy straight through to YouTube.
    `url` must already have passed url_security.validate_url() - this
    function does not re-validate it."""
    # No ffmpeg-level "-timeout"/"-rw_timeout" flag here: the exact option
    # name is protocol-specific (differs between http/rtmp/srt) and using
    # the wrong one makes ffprobe error out immediately. The asyncio
    # wait_for()+kill() below is a protocol-agnostic wall-clock timeout.
    # -i (rather than a trailing positional arg) is the unambiguous way to
    # pass the URL - url_security.validate_url() already rejected anything
    # starting with "-", so this can't be mistaken for another flag either way.
    argv = [
        config.FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-i", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=config.PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise ProbeError(f"probe timed out after {config.PROBE_TIMEOUT_SECONDS}s - is the URL reachable?")
    except FileNotFoundError:
        raise ProbeError("ffprobe not found on the dashboard host")

    if proc.returncode != 0:
        raise ProbeError((stderr.decode(errors="replace") or "ffprobe failed").strip()[-500:])

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        raise ProbeError("ffprobe returned unparseable output")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    video_codec = (video or {}).get("codec_name")
    audio_codec = (audio or {}).get("codec_name")
    copy_safe = (
        video is not None and video_codec in config.YOUTUBE_COMPATIBLE_VIDEO_CODECS
        and (audio is None or audio_codec in config.YOUTUBE_COMPATIBLE_AUDIO_CODECS)
    )
    return {
        "ok": True,
        "format_name": fmt.get("format_name"),
        "duration_seconds": float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None,
        "bitrate_kbps": round(int(fmt["bit_rate"]) / 1000) if fmt.get("bit_rate") else None,
        "video": {
            "codec": video_codec,
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        } if video else None,
        "audio": {
            "codec": audio_codec,
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
        } if audio else None,
        "copy_safe": copy_safe,
    }


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "N/A"):
        return None
    try:
        num, _, den = rate.partition("/")
        den = den or "1"
        return round(float(num) / float(den), 2) if float(den) != 0 else None
    except ValueError:
        return None
