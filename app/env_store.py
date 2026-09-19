"""Read/validate/write the streaming stack's .env. Reads go through a
fixed sudoers-allowed `cat`; writes go through the zoombot-owned
write-env.sh helper (atomic temp-file + rename + backup + chmod 600,
plus all KEY="value" quoting/escaping, done entirely inside that trusted
script, never in this process)."""
from __future__ import annotations

import re

from . import config
from .control import SUDO, run_as_zoombot, ControlError  # reuse the same sudo plumbing

CAT = "/usr/bin/cat"

# /j/ = ordinary meeting/webinar join link; /w/ = the per-registrant
# webinar link (carries tk=) - see app/zoomlink.py for the distinction.
ZOOM_LINK_RE = re.compile(r"^https://([a-z0-9.-]*\.)?zoom\.us/[jw]/(\d{9,11})(\?.*)?$", re.IGNORECASE)
PWD_PARAM_RE = re.compile(r"[?&]pwd=([^&]+)")


class ValidationError(Exception):
    pass


def _unquote_env_value(raw: str) -> str:
    """Reverses write-env.sh's KEY="value" quoting. Mirrors scripts/lib.sh's
    load_env_file() exactly (same escape convention, same unescape order -
    quote first, then backslash) so the dashboard's view of .env always
    matches what the streaming scripts actually load. Tolerates an
    unquoted or single-quoted value too, for a file not yet in the new
    format."""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1]
    return raw


def read_parsed() -> dict[str, str]:
    argv = [SUDO, "-u", config.ZOOMBOT_USER, CAT, str(config.STREAM_ENV_FILE)]
    proc = run_as_zoombot(argv)
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = _unquote_env_value(v.strip())
    return out


def masked_view() -> dict:
    current = read_parsed()
    view: dict = {}
    for key in config.NON_SECRET_ENV_KEYS:
        view[key] = current.get(key, "")
    view["ZOOM_LINK"] = current.get("ZOOM_LINK", "")
    view["ZOOM_PASSCODE"] = current.get("ZOOM_PASSCODE", "")
    yt = current.get("YT_STREAM_KEY", "")
    view["YT_STREAM_KEY"] = {"configured": bool(yt), "hint": f"ends in ••••{yt[-4:]}" if len(yt) >= 4 else ("configured" if yt else "not set")}
    vnc = current.get("VNC_PASSWORD", "")
    view["VNC_PASSWORD"] = {"configured": bool(vnc)}
    return view


def extract_zoom_id_passcode(link: str) -> tuple[str | None, str | None]:
    m = ZOOM_LINK_RE.match(link.strip())
    if not m:
        return None, None
    meeting_id = m.group(2)
    pwd_match = PWD_PARAM_RE.search(link)
    passcode = pwd_match.group(1) if pwd_match else None
    return meeting_id, passcode


def validate_zoom_link(link: str) -> None:
    if not ZOOM_LINK_RE.match(link.strip()):
        raise ValidationError(
            "Not a recognized Zoom link. Expected form: "
            "https://zoom.us/j/<meeting id>[?pwd=...]"
        )


def validate_updates(updates: dict[str, str]) -> None:
    if "ZOOM_LINK" in updates and updates["ZOOM_LINK"]:
        validate_zoom_link(updates["ZOOM_LINK"])
    if "RESOLUTION" in updates:
        m = re.match(r"^(\d+)x(\d+)$", updates["RESOLUTION"])
        if not m:
            raise ValidationError("RESOLUTION must look like 1920x1080")
        w, h = int(m.group(1)), int(m.group(2))
        r = config.RESOLUTION_RANGE
        if not (r["min_w"] <= w <= r["max_w"] and r["min_h"] <= h <= r["max_h"]):
            raise ValidationError(f"RESOLUTION out of allowed range {r}")
    if "FPS" in updates:
        try:
            fps = int(updates["FPS"])
        except ValueError:
            raise ValidationError("FPS must be an integer")
        lo, hi = config.FPS_RANGE
        if not (lo <= fps <= hi):
            raise ValidationError(f"FPS must be between {lo} and {hi}")
    for key, rng in (
        ("VIDEO_BITRATE", config.VIDEO_BITRATE_RANGE_KBPS),
        ("AUDIO_BITRATE", config.AUDIO_BITRATE_RANGE_KBPS),
    ):
        if key in updates:
            try:
                val = int(updates[key])
            except ValueError:
                raise ValidationError(f"{key} must be an integer (kbps)")
            lo, hi = rng
            if not (lo <= val <= hi):
                raise ValidationError(f"{key} must be between {lo} and {hi} kbps")
    if "BOT_NAME" in updates:
        name = updates["BOT_NAME"]
        if not name or len(name) > 64 or any(c in name for c in "\r\n"):
            raise ValidationError("BOT_NAME must be 1-64 characters, no newlines")
    if "ZOOM_SIGNIN_MODE" in updates and updates["ZOOM_SIGNIN_MODE"] not in config.ZOOM_SIGNIN_MODES:
        raise ValidationError(f"ZOOM_SIGNIN_MODE must be one of {sorted(config.ZOOM_SIGNIN_MODES)}")
    if "X264_PRESET" in updates and updates["X264_PRESET"] not in config.X264_PRESETS:
        raise ValidationError(f"X264_PRESET must be one of {config.X264_PRESETS}")
    unknown = set(updates) - set(config.ALL_ENV_KEYS)
    if unknown:
        raise ValidationError(f"Unknown config key(s): {', '.join(sorted(unknown))}")
    # write-env.sh refuses an embedded newline outright and would turn the
    # whole save into a 500; catch it here first with a clear message.
    # (CR alone is fine - write-env.sh strips it.)
    bad = [k for k, v in updates.items() if "\n" in v]
    if bad:
        raise ValidationError(f"Value(s) for {', '.join(sorted(bad))} contain a newline, which is not allowed")


def units_for_changed_keys(changed_keys: set[str]) -> list[str]:
    needed: set[str] = set()
    for key in changed_keys:
        for unit in config.ENV_KEY_RESTART_MAP.get(key, []):
            needed.add(unit)
    if "full-pipeline" in needed:
        return list(config.UNIT_ORDER)
    return sorted(needed)


def _encode_env_pairs(pairs: dict[str, str]) -> bytes:
    """NUL-delimited KEY/VALUE wire format expected by write-env.sh -
    see that script for why NUL (never newline) is the field separator."""
    parts: list[bytes] = []
    for key, val in pairs.items():
        parts.append(key.encode("utf-8"))
        parts.append(val.encode("utf-8"))
    return b"\x00".join(parts) + b"\x00"


def write_updates(updates: dict[str, str]) -> list[str]:
    """Validates, merges with current values, writes atomically via
    write-env.sh, and returns the list of units that need restarting."""
    validate_updates(updates)
    # VNC_PASSWORD never goes through this path - see control.rotate_vnc_password.
    updates = {k: v for k, v in updates.items() if k != "VNC_PASSWORD"}
    current = read_parsed()
    changed_keys = {k for k, v in updates.items() if current.get(k, "") != v}
    if not changed_keys:
        return []
    merged = {**current, **updates}
    content = _encode_env_pairs({key: merged.get(key, "") for key in config.ALL_ENV_KEYS})

    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.WRITE_ENV_SCRIPT)]
    proc = run_as_zoombot(argv, input_bytes=content, timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to write .env: " + proc.stderr.decode(errors="replace"))
    return units_for_changed_keys(changed_keys)
