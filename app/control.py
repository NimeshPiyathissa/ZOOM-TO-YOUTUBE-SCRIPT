"""Every privileged action the dashboard can take, in one place. Every
function here maps to a single, fixed, allowlisted command - argv lists
only, never shell=True, never user-supplied strings spliced into a
command. This is the *only* module allowed to call subprocess for
privileged operations."""
from __future__ import annotations

import functools
import pwd
import subprocess
import time

from . import config, url_security

SUDO = "/usr/bin/sudo"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
REBOOT = "/usr/sbin/reboot"
CAT = "/usr/bin/cat"

SHOW_PROPERTIES = (
    "ActiveState,SubState,Result,NRestarts,"
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainStatus,MainPID"
)

VERBS = {"start", "stop", "restart", "reset-failed"}

# The dashboard's single source of truth for "is it actually live" - every
# place that shows unit status (hero card, top badge, service card) must
# derive from this, never compute its own guess from raw active_state.
# Incident note: before this existed, the hero card and the ffmpeg-stream
# service card each made their own separate `systemctl show` call a few
# milliseconds apart, which could - and during the crash loop, did -
# disagree mid-flap. unit_show() below now makes exactly one call and
# both call sites read the same dict.
PHASE_STOPPED = "STOPPED"
PHASE_STARTING = "STARTING"
PHASE_LIVE = "LIVE"
PHASE_RECONNECTING = "RECONNECTING"
PHASE_FAILED = "FAILED"


def _derive_phase(active: str, sub: str, main_pid: int) -> str:
    if active == "failed":
        return PHASE_FAILED
    if active == "active" and sub == "running":
        # "active" alone isn't proof of life - insist on a real PID too.
        return PHASE_LIVE if main_pid > 0 else PHASE_STARTING
    if active == "active" and sub == "exited":
        # Type=oneshot + RemainAfterExit=yes (e.g. audio-setup): this is
        # its normal successful steady-state, not a transitional one -
        # there's no MainPID by design once the oneshot has exited.
        return PHASE_LIVE
    if active == "activating" and sub == "auto-restart":
        # Restart=on-failure is waiting out RestartSec before trying
        # again - this is a crash loop in progress, not "starting".
        return PHASE_RECONNECTING
    if active == "activating":
        return PHASE_STARTING
    return PHASE_STOPPED


class ControlError(Exception):
    pass


@functools.lru_cache(maxsize=1)
def zoombot_uid() -> int:
    return pwd.getpwnam(config.ZOOMBOT_USER).pw_uid


def _zoombot_env() -> dict:
    return {
        "XDG_RUNTIME_DIR": f"/run/user/{zoombot_uid()}",
        "PATH": "/usr/bin:/bin",
    }


def run_as_zoombot(argv: list[str], input_bytes: bytes | None = None, timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            env=_zoombot_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ControlError(f"command timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ControlError(f"command not found: {argv[0]}") from exc


def _require_unit(unit: str) -> None:
    if unit not in config.ALLOWED_UNITS:
        raise ControlError(f"unknown unit: {unit}")


def unit_action(unit: str, verb: str) -> dict:
    _require_unit(unit)
    if verb not in VERBS:
        raise ControlError(f"unknown verb: {verb}")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, SYSTEMCTL, "--user", verb, f"{unit}.service"]
    proc = run_as_zoombot(argv, timeout=25)
    return {
        "ok": proc.returncode == 0,
        "unit": unit,
        "verb": verb,
        "stderr": proc.stderr.decode(errors="replace").strip(),
    }


def unit_show(unit: str) -> dict:
    _require_unit(unit)
    argv = [
        SUDO, "-u", config.ZOOMBOT_USER, SYSTEMCTL, "--user", "show",
        f"{unit}.service", f"--property={SHOW_PROPERTIES}",
    ]
    proc = run_as_zoombot(argv)
    props: dict[str, str] = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    active = props.get("ActiveState", "unknown")
    sub = props.get("SubState", "unknown")
    ts = props.get("ActiveEnterTimestamp", "")
    main_pid = int(props.get("MainPID", "0") or 0)
    uptime_seconds = None
    if active == "active" and ts and ts not in ("0", ""):
        import datetime
        for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S %z"):
            try:
                parsed = datetime.datetime.strptime(ts, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                uptime_seconds = max(0, int(time.time() - parsed.timestamp()))
                break
            except ValueError:
                continue
    return {
        "unit": unit,
        "active_state": active,
        "sub_state": sub,
        "result": props.get("Result", ""),
        "restart_count": int(props.get("NRestarts", 0) or 0),
        "exec_main_status": props.get("ExecMainStatus", ""),
        "uptime_seconds": uptime_seconds,
        "main_pid": main_pid,
        "phase": _derive_phase(active, sub, main_pid),
    }


def unit_last_error(unit: str) -> str | None:
    _require_unit(unit)
    argv = [
        JOURNALCTL,
        f"_SYSTEMD_USER_UNIT={unit}.service",
        f"_UID={zoombot_uid()}",
        "-p", "err", "-n", "1", "--no-pager", "-o", "cat",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    line = proc.stdout.decode(errors="replace").strip()
    return line or None


def pipeline_stop() -> list[dict]:
    return [unit_action(unit, "stop") for unit in config.STOP_ORDER]


def pipeline_start() -> list[dict]:
    """Starts everything relevant to the *currently active* source (see
    start_source below for the normal switch-time path; this is the
    "restart whole pipeline" button, which re-derives the same shape from
    whatever's already in current-source.env rather than assuming zoom)."""
    source_type = read_current_source().get("SOURCE_TYPE", "zoom")
    results = []
    for unit in config.UNIT_ORDER:
        if unit in ("zoom", "browser-source") and config.PRODUCER_UNITS.get(source_type) != unit:
            continue
        if unit == "audio-setup" and source_type not in config.PRODUCER_UNITS:
            continue
        results.append(unit_action(unit, "start"))
        time.sleep(0.5)
    return results


def pipeline_restart() -> list[dict]:
    results = pipeline_stop()
    time.sleep(1)
    results += pipeline_start()
    return results


# ---------------------------------------------------------------- sources (Change 1)

SOURCE_ENV_KEYS = (
    "SOURCE_TYPE",
    "WEBPAGE_URL", "WEBPAGE_ZOOM", "WEBPAGE_RELOAD_SECONDS", "WEBPAGE_CLICK_TO_START",
    "DIRECT_URL", "DIRECT_MODE", "DIRECT_LOOP", "DIRECT_RECONNECT",
)


def _unquote_env_value(raw: str) -> str:
    """Reverses write-source.sh's KEY="value" quoting. Mirrors
    scripts/lib.sh's load_env_file() and env_store._unquote_env_value()
    exactly (same escape convention, same unescape order - quote first,
    then backslash) so this always matches what the streaming scripts
    actually load. Tolerates an unquoted or single-quoted value too, for
    a file not yet in the new format."""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1]
    return raw


def read_current_source() -> dict[str, str]:
    argv = [SUDO, "-u", config.ZOOMBOT_USER, CAT, str(config.SOURCE_ENV_FILE)]
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


def _source_env_lines(source: dict) -> dict[str, str]:
    """Builds the current-source.env content for a source dict as returned
    by app/sources.py. Re-validates the URL right here, immediately before
    it's written to a file a privileged script will read and hand to
    ffmpeg/Chromium - source.py already validated it at save time, but DNS
    can rebind between then and now (TOCTOU), so this is the real gate."""
    type_ = source["type"]
    options = source.get("options") or {}
    values = {k: "" for k in SOURCE_ENV_KEYS}
    values["SOURCE_TYPE"] = type_
    if type_ == "webpage":
        url = url_security.validate_url(source["url"], "webpage")
        values["WEBPAGE_URL"] = url
        values["WEBPAGE_ZOOM"] = str(options.get("zoom_level", 1.0))
        values["WEBPAGE_RELOAD_SECONDS"] = str(int(options.get("reload_seconds", 0)))
        values["WEBPAGE_CLICK_TO_START"] = "1" if options.get("click_to_start") else "0"
    elif type_ == "direct":
        url = url_security.validate_url(source["url"], "direct")
        values["DIRECT_URL"] = url
        values["DIRECT_MODE"] = options.get("mode", "reencode")
        values["DIRECT_LOOP"] = "1" if options.get("loop") else "0"
        values["DIRECT_RECONNECT"] = "1" if options.get("reconnect", True) else "0"
    return values


def write_current_source(source: dict) -> None:
    values = _source_env_lines(source)
    bad = [k for k, v in values.items() if "\n" in v]
    if bad:
        raise ControlError(f"value(s) for {', '.join(sorted(bad))} contain a newline, which is not allowed")
    # NUL-delimited KEY/VALUE wire format expected by write-source.sh -
    # see that script for why NUL (never newline) is the field separator.
    parts: list[bytes] = []
    for key, val in values.items():
        parts.append(key.encode("utf-8"))
        parts.append(val.encode("utf-8"))
    content = b"\x00".join(parts) + b"\x00"
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.WRITE_SOURCE_SCRIPT)]
    proc = run_as_zoombot(argv, input_bytes=content, timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to write current-source.env: " + proc.stderr.decode(errors="replace"))


def apply_source_config(source: dict) -> None:
    """Writes `source`'s config to .env (if zoom) and current-source.env,
    without starting or stopping anything - the config-only half of
    start_source, split out so scheduled jobs can pick a source and
    separately decide go_live vs stop (see scheduler.py), same as the
    old profile-switch-then-act semantics."""
    type_ = source["type"]
    if type_ not in config.SOURCE_TYPES:
        raise ControlError(f"unknown source type: {type_}")

    if type_ == "zoom":
        from . import env_store  # local import: env_store imports from this module
        options = source.get("options") or {}
        env_store.write_updates({
            "ZOOM_LINK": source["url"],
            "ZOOM_PASSCODE": options.get("passcode", ""),
            "BOT_NAME": options.get("bot_name", "Stream Bot"),
            "ZOOM_SIGNIN_MODE": options.get("signin_mode", "guest"),
        })

    write_current_source(source)


def start_source(source: dict) -> list[dict]:
    """Switch to `source` and go live with it: quick-reconnect style (stop
    the encoder, swap the producer, start the encoder again) - the same
    few seconds of YouTube-side buffering as today's "Restart encoder"
    button, not a hot swap."""
    type_ = source["type"]
    apply_source_config(source)

    results = [unit_action("ffmpeg-stream", "stop")]
    for other_type, other_unit in config.PRODUCER_UNITS.items():
        if other_type != type_:
            results.append(unit_action(other_unit, "stop"))

    if type_ in config.PRODUCER_UNITS:
        results.append(unit_action("xvfb", "start")); time.sleep(0.5)
        results.append(unit_action("openbox", "start")); time.sleep(0.5)
        results.append(unit_action("audio-setup", "start")); time.sleep(0.5)
        results.append(unit_action(config.PRODUCER_UNITS[type_], "start")); time.sleep(1)
        results.append(unit_action("x11vnc", "start"))

    results.append(unit_action("ffmpeg-stream", "start"))
    return results


def rotate_vnc_password(new_password: str) -> dict:
    if not (4 <= len(new_password) <= 128):
        raise ControlError("password must be 4-128 characters")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.ROTATE_VNC_SCRIPT)]
    proc = run_as_zoombot(argv, input_bytes=new_password.encode("utf-8"), timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to set VNC password")
    return unit_action("x11vnc", "restart")


def run_test_recording(timeout: int = 75) -> dict:
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.TEST_RECORDING_SCRIPT)]
    proc = run_as_zoombot(argv, timeout=timeout)
    return {
        "ok": proc.returncode == 0,
        "stderr": proc.stderr.decode(errors="replace")[-2000:],
    }


def reboot_vps() -> None:
    subprocess.run([SUDO, REBOOT], timeout=10)


# ---------------------------------------------------------------- Zoom Google sign-in (Change 2)
#
# No credential automation lives here or anywhere else in this codebase:
# these functions only ever open Zoom's own sign-in screen for a human to
# complete over noVNC, or ask Zoom to forget its saved session. Neither a
# Google password, a 2FA code, nor an OAuth token is ever read, stored, or
# logged by the dashboard - the "signed in as" label shown in the UI is
# text the admin types in themselves after finishing sign-in by hand.

# Filesystem markers that suggest the Zoom client currently holds a saved
# session. This is a heuristic, not an authoritative check - Zoom doesn't
# expose a documented "am I logged in" query - so callers should always
# treat it as a hint alongside the admin-confirmed label, never on its own.
_ZOOM_SESSION_MARKER_PATHS = (
    f"{config.ZOOMBOT_HOME}/.zoom/data",
    f"{config.ZOOMBOT_HOME}/.config/zoomus.conf",
)


def zoom_google_signin() -> dict:
    """Brings up the display/window-manager/VNC infra (if not already up)
    and launches the Zoom client to its own sign-in screen - no deep-link
    auto-join - so a human can complete Google OAuth over noVNC."""
    unit_action("xvfb", "start")
    unit_action("openbox", "start")
    unit_action("audio-setup", "start")
    unit_action("x11vnc", "start")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.ZOOM_SIGNIN_SCRIPT)]
    proc = run_as_zoombot(argv, timeout=20)
    if proc.returncode != 0:
        raise ControlError("failed to launch Zoom sign-in: " + proc.stderr.decode(errors="replace"))
    return {"ok": True}


def zoom_signout() -> dict:
    """Asks the Zoom client to forget its saved session. Does not touch
    the Chromium profile (that's shared with webpage sources and has its
    own, separate "forget site data" path if ever needed)."""
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.ZOOM_SIGNOUT_SCRIPT)]
    proc = run_as_zoombot(argv, timeout=20)
    if proc.returncode != 0:
        raise ControlError("failed to sign Zoom out: " + proc.stderr.decode(errors="replace"))
    return {"ok": True}


def zoom_session_heuristic() -> dict:
    """Best-effort, non-authoritative signal only - see module note above."""
    found = False
    for p in _ZOOM_SESSION_MARKER_PATHS:
        try:
            proc = run_as_zoombot([SUDO, "-u", config.ZOOMBOT_USER, "/usr/bin/test", "-e", p], timeout=5)
        except ControlError:
            continue
        if proc.returncode == 0:
            found = True
            break
    return {"session_files_present": found, "authoritative": False}
