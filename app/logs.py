"""Live log tailing (journal for most units, the dedicated log file for
zoom/ffmpeg-stream where the useful detail actually lands) with secret
redaction applied to every line before it ever leaves this process."""
from __future__ import annotations

import asyncio
import re

from . import config, env_store
from .control import zoombot_uid, unit_last_error

JOURNALCTL = "/usr/bin/journalctl"

FILE_BACKED_UNITS = {
    "zoom": config.ZOOM_LOG,
    "ffmpeg-stream": config.FFMPEG_LOG,
    "browser-source": config.BROWSER_LOG,
}

_REDACT_PATTERNS = [
    re.compile(r"(pwd=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(rtmps?://[^/\s]+/live2/)[^\s\"']+", re.IGNORECASE),
]


def build_redactor() -> callable:
    """Snapshot current secret values once per call and mask any literal
    occurrence of them, plus generic pattern-based redaction as a
    defense-in-depth fallback (e.g. if a value was just rotated)."""
    current = env_store.read_parsed()
    literals = [current.get(k, "") for k in config.SECRET_ENV_KEYS if current.get(k)]
    literals = [v for v in literals if len(v) >= 4]

    def redact(line: str) -> str:
        for secret in literals:
            if secret in line:
                line = line.replace(secret, "<redacted>")
        for pattern in _REDACT_PATTERNS:
            line = pattern.sub(r"\1<redacted>", line)
        return line

    return redact


async def tail_file(path, redact) -> "asyncio.AsyncIterator[str]":
    if not path.exists():
        yield f"(no log yet: {path.name})"
        return
    with open(path, "r", errors="replace") as f:
        # last ~200 lines of backlog
        lines = f.readlines()[-200:]
        for line in lines:
            yield redact(line.rstrip("\n"))
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                yield redact(line.rstrip("\n"))
            else:
                await asyncio.sleep(0.5)


async def tail_journal(unit: str, redact) -> "asyncio.AsyncIterator[str]":
    argv = [
        JOURNALCTL,
        f"_SYSTEMD_USER_UNIT={unit}.service",
        f"_UID={zoombot_uid()}",
        "-f", "-n", "100", "--no-pager", "-o", "short-iso",
    ]
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE)
    try:
        assert proc.stdout
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            yield redact(raw.decode(errors="replace").rstrip("\n"))
    finally:
        if proc.returncode is None:
            proc.kill()


def tail_for_unit(unit: str):
    redact = build_redactor()
    if unit in FILE_BACKED_UNITS:
        return tail_file(FILE_BACKED_UNITS[unit], redact)
    return tail_journal(unit, redact)


def recent_errors() -> list[dict]:
    redact = build_redactor()
    out = []
    for unit in config.VISIBLE_UNITS:
        line = unit_last_error(unit)
        if line:
            out.append({"unit": unit, "message": redact(line)})
    return out
