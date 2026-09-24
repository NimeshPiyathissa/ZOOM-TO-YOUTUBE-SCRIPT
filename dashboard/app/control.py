"""Every privileged action the dashboard can take, in one place. Every
function here maps to a single, fixed, allowlisted command - argv lists
only, never shell=True, never user-supplied strings spliced into a
command. This is the *only* module allowed to call subprocess for
privileged operations."""
from __future__ import annotations

import functools
import json
import pwd
import re
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
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainStatus,MainPID,ExecMainCode"
)

# ffmpeg exits 255 only when it caught a stop signal and shut down cleanly
# (see systemd/ffmpeg-stream.service in the zoom-stream repo, which also
# tells systemd this via SuccessExitStatus=255). Kept here too so the
# dashboard classifies the historical state correctly even before that
# unit change is loaded, and so this can never drift from the unit file
# into showing a deliberate stop as a failure.
_FFMPEG_CLEAN_STOP_EXIT_STATUS = "255"

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


def _derive_phase(active: str, sub: str, main_pid: int, unit: str = "",
                  exec_main_code: str = "", exec_main_status: str = "") -> str:
    if active == "failed":
        # A deliberate stop that ffmpeg acknowledged (exited, not killed,
        # with its signal-exit code) is Stopped, not Failed - this was the
        # "ERROR badge but the logs page says no recent errors" bug.
        if (unit == "ffmpeg-stream" and exec_main_code == "1"
                and exec_main_status == _FFMPEG_CLEAN_STOP_EXIT_STATUS):
            return PHASE_STOPPED
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
        "DISPLAY": config.DISPLAY_NUM,
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
    res = {
        "ok": proc.returncode == 0,
        "unit": unit,
        "verb": verb,
        "stderr": proc.stderr.decode(errors="replace").strip(),
    }
    if res["ok"] and unit == "ffmpeg-stream":
        try:
            from . import telegram
            if verb in ("start", "restart"):
                telegram.alert_stream_started()
            elif verb == "stop":
                telegram.alert_stream_stopped()
        except Exception:
            pass
    return res


def record_stream_action(action: str) -> dict:
    """Controls local MP4 recording: start | stop | status.
    Saves compressed archive to /home/zoombot/recordings/ with 2.0 GB safety halt.
    """
    if action not in ("start", "stop", "status"):
        raise ControlError(f"invalid record-stream action: {action}")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.RECORD_STREAM_SCRIPT), action]
    proc = run_as_zoombot(argv, timeout=25)
    out = proc.stdout.decode(errors="replace").strip()
    if not out:
        err = proc.stderr.decode(errors="replace").strip()
        raise ControlError(f"record-stream {action} failed: {err}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise ControlError(f"record-stream returned invalid JSON: {out}")



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
    exec_main_code = props.get("ExecMainCode", "")
    exec_main_status = props.get("ExecMainStatus", "")
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
        "exec_main_status": exec_main_status,
        "exec_main_code": exec_main_code,
        "uptime_seconds": uptime_seconds,
        "main_pid": main_pid,
        "phase": _derive_phase(active, sub, main_pid, unit, exec_main_code, exec_main_status),
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
    env = read_current_source()
    source_type = env.get("SOURCE_TYPE", "zoom")
    if source_type == "webpage":
        wanted_unit = "browser-source"
    elif source_type == "zoom":
        wanted_unit = "browser-source" if env.get("ZOOM_JOIN_VIA") == "web" else "zoom"
    else:
        wanted_unit = None
    results = []
    for unit in config.UNIT_ORDER:
        if unit in ("zoom", "browser-source") and unit != wanted_unit:
            continue
        if unit == "audio-setup" and wanted_unit is None:
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
    # Part 3: Chrome profile of the bound Google account (see
    # app/accounts.py); read by browser-source.sh and
    # open-url-with-account.sh. Empty = shared stream profile.
    "ACCOUNT_PROFILE_ID",
    # Zoom page: per-meeting join policy read by join-zoom.sh. Not secret
    # (the link/passcode stay in .env). ZOOM_JOIN_EPOCH changes on every
    # operator-initiated join so the script's rejoin counter resets.
    "ZOOM_AUTO_REJOIN", "ZOOM_REJOIN_MAX", "ZOOM_JOIN_EPOCH",
    "ZOOM_AUDIO_ON", "ZOOM_VIDEO_ON", "ZOOM_VIEW",
    # "client" (desktop, join-zoom.sh) or "web" (Zoom's web client, run by
    # browser-source.sh against the same ZOOM_LINK/ZOOM_PASSCODE already
    # in .env - no separate secret storage needed). Not secret itself.
    # join_method=auto starts this at "client"; a mechanism-level failure
    # flips it to "web" mid-join via switch_zoom_join_via(), never a fresh
    # apply_source_config (that would also reset the rejoin epoch).
    "ZOOM_JOIN_VIA",
)

# Part 4: the real (encoder-burned) watermark filter's config - see
# scripts/lib.sh's build_watermark_filter(), which stream.sh and
# test-recording.sh both call. Lives in current-source.env (not .env)
# because it's not secret, exactly like every other WATERMARK_ENV_KEYS
# concern - but unlike SOURCE_ENV_KEYS above, these are independent of
# which source is active, so _source_env_lines() below must carry them
# forward across a source switch rather than resetting them to "".
WATERMARK_ENV_KEYS = (
    "WATERMARK_ENABLED", "WATERMARK_MODE", "WATERMARK_TEXT", "WATERMARK_FONT",
    "WATERMARK_ANCHOR", "WATERMARK_MARGIN_X", "WATERMARK_MARGIN_Y",
    "WATERMARK_SIZE", "WATERMARK_OPACITY", "WATERMARK_IMAGE_PATH",
)
SOURCE_ENV_KEYS = SOURCE_ENV_KEYS + WATERMARK_ENV_KEYS


def _account_profile_id(source: dict) -> str:
    account_id = source.get("account_id")
    if not account_id:
        return ""
    from . import db  # local: avoid a module-level cycle through accounts
    with db.get_conn() as conn:
        row = conn.execute("SELECT profile_id FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row["profile_id"] if row else ""


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
    values["ACCOUNT_PROFILE_ID"] = _account_profile_id(source)
    # Watermark config is independent of which source is active - carry
    # it forward from whatever's already on disk rather than resetting it
    # every time a source is applied (switching sources would otherwise
    # silently clear the watermark).
    current = read_current_source()
    for key in WATERMARK_ENV_KEYS:
        if key in current:
            values[key] = current[key]
    if type_ == "zoom":
        values["ZOOM_AUTO_REJOIN"] = "1" if options.get("auto_rejoin", True) else "0"
        values["ZOOM_REJOIN_MAX"] = str(int(options.get("rejoin_max", 5)))
        values["ZOOM_JOIN_EPOCH"] = str(int(time.time()))
        values["ZOOM_AUDIO_ON"] = "1" if options.get("audio_on") else "0"
        values["ZOOM_VIDEO_ON"] = "1" if options.get("video_on") else "0"
        values["ZOOM_VIEW"] = str(options.get("view", "speaker"))
        # auto always starts on the client - the fallback watcher (Part 4)
        # flips this to "web" mid-join if the client path never gets off
        # the ground; it never starts on web first.
        values["ZOOM_JOIN_VIA"] = "web" if options.get("join_method") == "web" else "client"
    elif type_ == "webpage":
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


def _write_source_env_values(values: dict[str, str]) -> None:
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


def write_current_source(source: dict) -> None:
    _write_source_env_values(_source_env_lines(source))


def write_watermark_config(watermark: dict) -> None:
    """Updates only WATERMARK_ENV_KEYS in current-source.env, leaving the
    active source's own keys untouched - the inverse of how
    _source_env_lines() preserves watermark keys across a source switch.
    `watermark` is app/overlay.py's state dict (get_overlay_state());
    margin/size/opacity are written as plain integers, never trusting the
    caller to have already stringified them correctly."""
    current = read_current_source()
    values = {k: current.get(k, "") for k in SOURCE_ENV_KEYS}
    values["WATERMARK_ENABLED"] = "1" if watermark.get("visible") else "0"
    values["WATERMARK_MODE"] = str(watermark.get("mode", "text"))
    values["WATERMARK_TEXT"] = str(watermark.get("text", ""))
    values["WATERMARK_FONT"] = str(watermark.get("encoder_font", "inter"))
    values["WATERMARK_ANCHOR"] = str(watermark.get("anchor", "bottom-right"))
    values["WATERMARK_MARGIN_X"] = str(int(watermark.get("margin_x", 24)))
    values["WATERMARK_MARGIN_Y"] = str(int(watermark.get("margin_y", 24)))
    size = watermark.get("image_scale_pct") if watermark.get("mode") == "image" else watermark.get("font_size")
    values["WATERMARK_SIZE"] = str(size if size is not None else 28)
    opacity = watermark.get("image_opacity") if watermark.get("mode") == "image" else watermark.get("font_opacity")
    values["WATERMARK_OPACITY"] = str(int(opacity if opacity is not None else 100))
    values["WATERMARK_IMAGE_PATH"] = str(watermark.get("image_path", ""))
    _write_source_env_values(values)


def write_watermark_image(content: bytes, ext: str) -> str:
    """Pipes raw image bytes to the zoombot-owned write-watermark-image.sh
    (same sudo/stdin-piping contract as every other zoombot write in this
    file) and returns the path it wrote - which becomes WATERMARK_IMAGE_PATH
    in current-source.env. The extension is passed as an argv element (not
    secret, and validated server-side both here and again in the script -
    never derived from raw user input beyond that fixed allowlist)."""
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise ControlError(f"unsupported image extension: {ext}")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.WRITE_WATERMARK_IMAGE_SCRIPT), ext]
    proc = run_as_zoombot(argv, input_bytes=content, timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to write watermark image: " + proc.stderr.decode(errors="replace"))
    return proc.stdout.decode(errors="replace").strip()


def watermark_is_running() -> bool:
    """Real state, not "is it saved to overlay.json": does the *currently
    running* ffmpeg-stream process actually have a watermark filter in its
    command line right now. Used so the dashboard's toggle can honestly
    show whether a saved change has actually taken effect yet, instead of
    just echoing back what was last written (see Part 4's UI requirement
    - a config change here needs an encoder restart, and the UI must say
    so rather than silently no-op).
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-a", "-u", config.ZOOMBOT_USER, "ffmpeg"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if proc.returncode != 0:
        return False
    # The full command line includes the YouTube RTMP URL (stream key and
    # all, same as ffmpeg.log) - decoded only to check for these two
    # substrings and immediately discarded. Never log, return, or
    # otherwise let `cmdline` escape this function.
    cmdline = proc.stdout.decode(errors="replace")
    return "drawtext=" in cmdline or "overlay=" in cmdline


def switch_zoom_join_via(via: str) -> None:
    """Flips ZOOM_JOIN_VIA in current-source.env in place, leaving every
    other key (join epoch, rejoin counters, account profile) untouched.
    Used only by the join_method=auto fallback (Part 4) - that's a
    continuation of the same operator-initiated join, not a fresh one, so
    it must not reset ZOOM_JOIN_EPOCH the way a full apply_source_config
    would."""
    if via not in ("client", "web"):
        raise ControlError(f"invalid join_via: {via}")
    current = read_current_source()
    if not current:
        raise ControlError("current-source.env is empty or unreadable")
    values = {k: current.get(k, "") for k in SOURCE_ENV_KEYS}
    values["ZOOM_JOIN_VIA"] = via
    _write_source_env_values(values)


def producer_unit_for(source: dict | None) -> str | None:
    """Which systemd unit actually draws `source`'s content onto :99.
    zoom with join_method=client (or auto, before any fallback) -> the
    zoom unit (scripts/join-zoom.sh, desktop deep-link join). zoom with
    join_method=web (or auto, after a fallback) and webpage -> the
    browser-source unit (Chrome kiosk - either a generic page or Zoom's
    own web client, see scripts/browser-source.sh). direct sources, and
    no active source, use neither (ffmpeg reads a direct URL itself)."""
    if not source:
        return None
    type_ = source.get("type")
    if type_ == "webpage":
        return "browser-source"
    if type_ != "zoom":
        return None
    method = (source.get("options") or {}).get("join_method", "client")
    return "browser-source" if method == "web" else "zoom"


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
        from . import env_store, sources as sources_mod  # local: both import from this module
        options = source.get("options") or {}
        join_url = sources_mod.effective_zoom_join_url(source)
        if not join_url:
            raise ControlError(
                "This Zoom source is a registration page with no personal join link saved yet - "
                "open the registration, complete it, then save the join link Zoom gives you."
            )
        env_store.write_updates({
            "ZOOM_LINK": join_url,
            "ZOOM_PASSCODE": options.get("passcode", ""),
            "BOT_NAME": options.get("bot_name", "Stream Bot"),
            "ZOOM_SIGNIN_MODE": options.get("signin_mode", "guest"),
        })

    write_current_source(source)


def _ffmpeg_is_up() -> bool:
    try:
        return unit_show("ffmpeg-stream")["phase"] in (PHASE_LIVE, PHASE_STARTING, PHASE_RECONNECTING)
    except ControlError:
        return False


def _set_slate() -> dict:
    proc = run_as_zoombot([SUDO, "-u", config.ZOOMBOT_USER, XSETROOT_BIN, "-solid", SLATE_COLOR], timeout=10)
    return {"ok": proc.returncode == 0, "action": "slate"}


def start_source(source: dict) -> dict:
    """Switch to `source`, keeping the RTMP connection up whenever that's
    physically possible:

    - webpage -> webpage while the kiosk Chrome is running: navigate the
      existing tab over DevTools (app/cdp.py). Nothing restarts.
    - zoom <-> webpage: ffmpeg captures :99 + zoom_out.monitor regardless
      of which app is drawing, so the encoder keeps running; only the
      producer (zoom / browser-source) is swapped, behind a plain slate.
      Previously this path stopped and restarted ffmpeg too - that was a
      choice, not a requirement, and it dropped RTMP every time.
    - anything involving a direct-media source: ffmpeg's input graph is
      different, so the encoder must restart. The caller is told via
      rtmp_dropped=True (the UI confirms first while live) and a slate is
      shown for the gap.
    Returns {"results": [...], "rtmp_dropped": bool, "hot_swapped": bool}."""
    type_ = source["type"]
    old_type = read_current_source().get("SOURCE_TYPE", "")
    ffmpeg_up = _ffmpeg_is_up()
    results: list[dict] = []

    # Fastest path: same producer, just a new URL.
    if type_ == "webpage" and old_type == "webpage" and ffmpeg_up:
        try:
            if unit_show("browser-source")["phase"] == PHASE_LIVE:
                import asyncio
                from . import cdp, url_security
                url = url_security.validate_url(source["url"], "webpage")
                asyncio.run(cdp.navigate(url))
                apply_source_config(source)  # so a later restart lands on the same page
                return {"results": [{"ok": True, "action": "cdp-navigate"}], "rtmp_dropped": False, "hot_swapped": True}
        except Exception as exc:  # CDPError (no debug port yet), URLSecurityError, ControlError
            results.append({"ok": False, "action": "cdp-navigate", "stderr": str(exc)[:200]})
            # fall through to a producer restart, which still keeps RTMP up

    apply_source_config(source)
    wanted_unit = producer_unit_for(source)
    needs_ffmpeg_restart = type_ == "direct" or old_type == "direct"
    rtmp_dropped = needs_ffmpeg_restart and ffmpeg_up

    if ffmpeg_up:
        results.append(_set_slate())
    if needs_ffmpeg_restart:
        results.append(unit_action("ffmpeg-stream", "stop"))
    # zoom and browser-source are mutually exclusive producers - a zoom
    # source with join_method=web needs browser-source, not the zoom
    # unit, so this compares against the resolved unit rather than type_.
    for u in ("zoom", "browser-source"):
        if u != wanted_unit:
            results.append(unit_action(u, "stop"))

    if wanted_unit:
        results.append(unit_action("xvfb", "start")); time.sleep(0.5)
        results.append(unit_action("openbox", "start")); time.sleep(0.5)
        results.append(unit_action("audio-setup", "start")); time.sleep(0.5)
        results.append(unit_action(wanted_unit, "restart")); time.sleep(1)
        results.append(unit_action("x11vnc", "start"))

    if needs_ffmpeg_restart or not ffmpeg_up:
        results.append(unit_action("ffmpeg-stream", "start"))
    return {"results": results, "rtmp_dropped": rtmp_dropped, "hot_swapped": False}


def rotate_vnc_password(new_password: str) -> dict:
    """Sets x11vnc's own password store, then updates VNC_PASSWORD's
    source of truth. Part 0: that's the encrypted vault (if unlocked) -
    never settings.json in plaintext again. On an install that hasn't run
    vault setup yet, falls back to the legacy settings_store write so VNC
    access still works until it has."""
    if not (4 <= len(new_password) <= 128):
        raise ControlError("password must be 4-128 characters")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.ROTATE_VNC_SCRIPT)]
    proc = run_as_zoombot(argv, input_bytes=new_password.encode("utf-8"), timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to set VNC password")
    try:
        from . import secret_store
        if secret_store.is_unlocked():
            secret_store.set_secrets({"VNC_PASSWORD": new_password})
        else:
            raise secret_store.VaultLockedError
    except Exception:
        try:
            from . import settings_store
            settings_store.save_settings({"vnc_password": new_password})
        except Exception:
            pass
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


# ---------------------------------------------------------------- accounts (Part 2)

ACCOUNT_SCRIPT = config.STREAM_SCRIPTS_DIR / "chrome-account.sh"
ACCOUNT_ACTIONS = {"create", "signin", "close", "status", "verify", "remove",
                   "import", "signout", "backup", "restore", "backups"}
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_BACKUP_NAME_RE = re.compile(r"^[a-z0-9-]+-\d{8}-\d{6}\.tar\.gz$")


def account_profile_action(action: str, profile_id: str, timeout: int = 20, extra: str | None = None) -> str:
    """Runs scripts/chrome-account.sh as zoombot. The dashboard never
    touches the profile directory itself (cookies live there); it only
    ever gets this script's one-line result back. No credential is
    passed in or out - see that script's header. `extra` is the one
    optional trailing argument some actions take (a backup file name for
    restore), validated here before it becomes argv."""
    if action not in ACCOUNT_ACTIONS:
        raise ControlError(f"invalid account action: {action}")
    if not _PROFILE_ID_RE.match(profile_id):
        raise ControlError("invalid profile id")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(ACCOUNT_SCRIPT), action, profile_id]
    if extra:
        if action != "restore" or not _BACKUP_NAME_RE.match(extra) or not extra.startswith(profile_id + "-"):
            raise ControlError("invalid backup name")
        argv.append(extra)
    proc = run_as_zoombot(argv, timeout=timeout)
    if proc.returncode != 0:
        raise ControlError(proc.stderr.decode(errors="replace").strip() or f"account {action} failed")
    return proc.stdout.decode(errors="replace").strip()


# ---------------------------------------------------------------- touch remote / media control (Part 3)

STREAM_AUDIO_SCRIPT = config.STREAM_SCRIPTS_DIR / "set-stream-audio.sh"
PYTHON3_BIN = "/usr/bin/python3"
XSETROOT_BIN = "/usr/bin/xsetroot"
SLATE_COLOR = "#0b0f14"  # matches the dashboard's own dark background token


def stream_audio_action(action: str, volume: int | None = None) -> dict:
    """Mutes/unmutes or sets the volume of what viewers hear, via the
    zoom_out sink ffmpeg captures from - never touches ffmpeg or the RTMP
    connection. Always returns the real state read back from pactl, not
    the state the caller asked for, in case the write silently didn't
    take. Returns {"muted": bool, "volume": 0-150, "sink_state":
    RUNNING|IDLE|SUSPENDED, "input_streams": n} - the last two are how
    the mixer tells "nothing is playing into the sink" (SUSPENDED / 0)
    apart from "something is playing but it's quiet"."""
    if action not in ("mute", "unmute", "status", "volume"):
        raise ControlError(f"invalid stream-audio action: {action}")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(STREAM_AUDIO_SCRIPT), action]
    if action == "volume":
        if volume is None or not (0 <= int(volume) <= 150):
            raise ControlError("volume must be 0-150")
        argv.append(str(int(volume)))
    proc = run_as_zoombot(argv, timeout=10)
    out = proc.stdout.decode(errors="replace").strip().split()
    if proc.returncode != 0 or not out or out[0] not in ("muted", "unmuted"):
        raise ControlError("failed to read/set stream audio: " + proc.stderr.decode(errors="replace").strip())
    vol = int(out[1]) if len(out) > 1 and out[1].isdigit() else 100
    sink_state = out[2] if len(out) > 2 else "UNKNOWN"
    streams = int(out[3]) if len(out) > 3 and out[3].isdigit() else 0
    return {"muted": out[0] == "muted", "volume": vol, "sink_state": sink_state, "input_streams": streams}


AUDIO_SELFTEST_SCRIPT = config.STREAM_SCRIPTS_DIR / "audio-selftest.sh"


def audio_selftest() -> dict:
    """Tone -> zoom_out -> meter + ffmpeg capture -> file -> volumedetect.
    The script itself refuses to run while ffmpeg-stream is active (the
    tone would go out to viewers), so this never needs a force path."""
    proc = run_as_zoombot([SUDO, "-u", config.ZOOMBOT_USER, str(AUDIO_SELFTEST_SCRIPT)], timeout=60)
    try:
        data = json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ControlError("audio self-test produced no result: " + proc.stderr.decode(errors="replace")[-300:])
    return data


# --- Zoom readback: AT-SPI, and honest about it. --------------------------
#
# Zoom's Linux client has no local API; the accessibility tree (exposed
# because systemd/zoom.service sets QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1,
# confirmed live on this box 2026-09-19) is the only way to read Zoom's
# *own* idea of its state instead of assuming a keystroke landed. Every
# function here returns state="unknown" with a reason rather than a
# guess when the control can't be found.

ZOOM_SHORTCUT_SCRIPT = config.STREAM_SCRIPTS_DIR / "zoom-shortcut.sh"
ZOOM_ATSPI_SCRIPT = config.STREAM_SCRIPTS_DIR / "zoom-atspi.py"
ZOOM_STATUS_SCRIPT = config.STREAM_SCRIPTS_DIR / "zoom-status.py"
ZOOM_DIALOG_SCRIPT = config.STREAM_SCRIPTS_DIR / "zoom-dialog.py"
ZOOM_SHORTCUT_ACTIONS = {"mic", "camera", "view-speaker", "view-gallery"}
ZOOM_ATSPI_QUERIES = {"mic", "camera", "buttons"}


def _zoom_atspi(query: str) -> dict:
    """{"available": bool, "state": muted|unmuted|no_audio|on|off|unknown,
    "name": <Zoom's own button label>, "reason": <why unavailable>}."""
    if query not in ZOOM_ATSPI_QUERIES:
        raise ControlError(f"invalid atspi query: {query}")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, PYTHON3_BIN, str(ZOOM_ATSPI_SCRIPT), query]
    try:
        proc = run_as_zoombot(argv, timeout=12)
        data = json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (ControlError, json.JSONDecodeError, IndexError):
        return {"available": False, "state": "unknown", "reason": "check did not run"}
    data.setdefault("available", False)
    data.setdefault("state", "unknown")
    data["authoritative"] = False
    return data


def zoom_mic_state() -> dict:
    """Zoom's reported mic state: muted / unmuted / no_audio (bot hasn't
    joined audio, so it can't be unmuted) / unknown."""
    return _zoom_atspi("mic")


def zoom_shortcut(action: str) -> dict:
    """Sends one of Zoom's own keyboard shortcuts to the meeting window
    (Alt+A mic, Alt+V camera, Alt+F1/F2 view), then reads the resulting
    state back from Zoom's accessibility tree. `verify.available` says
    whether that readback means anything right now; `verify.state` is
    what Zoom reports, never what we assume."""
    if action not in ZOOM_SHORTCUT_ACTIONS:
        raise ControlError(f"invalid zoom action: {action}")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(ZOOM_SHORTCUT_SCRIPT), action]
    proc = run_as_zoombot(argv, timeout=10)
    if proc.returncode != 0:
        raise ControlError(proc.stderr.decode(errors="replace").strip() or "zoom shortcut failed")
    time.sleep(0.5)
    if action == "mic":
        verify = _zoom_atspi("mic")
    elif action == "camera":
        verify = _zoom_atspi("camera")
    else:
        verify = {"available": False, "state": "unknown", "reason": "view mode has no readable state"}
    return {"sent": True, "action": action, "verify": verify}


def zoom_mic_toggle() -> dict:
    """Mixer-strip mic button: Alt+A to the Zoom window, then Zoom's own
    reported state. Returns {"sent": True, "verify": {...state...}}."""
    return zoom_shortcut("mic")


def _zoom_meeting_status_client() -> dict:
    """The desktop-client path: AT-SPI text heuristics, as before."""
    argv = [SUDO, "-u", config.ZOOMBOT_USER, PYTHON3_BIN, str(ZOOM_STATUS_SCRIPT)]
    try:
        proc = run_as_zoombot(argv, timeout=15)
        data = json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (ControlError, json.JSONDecodeError, IndexError):
        data = {"status": "unknown", "detail": "status check did not run", "dialogs": [], "terminal": False}
    try:
        data["service"] = unit_show("zoom")["phase"]
    except ControlError:
        data["service"] = "unknown"
    if data["status"] in ("in_meeting", "waiting_room", "not_started", "connecting"):
        data["mic"] = _zoom_atspi("mic")
        data["camera"] = _zoom_atspi("camera")
    else:
        # No meeting window -> no toolbar -> nothing to read; say so
        # without spending two AT-SPI walks on it.
        data["mic"] = {"available": False, "state": "unknown", "reason": "not in a meeting"}
        data["camera"] = {"available": False, "state": "unknown", "reason": "not in a meeting"}
    return data


def _zoom_meeting_status_web() -> dict:
    """The web-client path: DOM text read over CDP (app/zoom_web.py).
    Camera/mic are never automated for this path (see zoom_web.py), so
    they're reported as not applicable rather than unknown - there is
    nothing to read that would ever say otherwise."""
    from . import zoom_web
    data = zoom_web.status()
    try:
        data["service"] = unit_show("browser-source")["phase"]
    except ControlError:
        data["service"] = "unknown"
    data["mic"] = {"available": False, "state": "unknown", "reason": "not automated for the web-client path"}
    data["camera"] = {"available": False, "state": "unknown", "reason": "not automated for the web-client path"}
    return data


def zoom_meeting_status() -> dict:
    """not_joined / connecting / waiting_room / in_meeting / ended /
    expired / passcode_required / registration_required / removed /
    join_failed / duplicate_join / wrong_registrant / unknown, plus
    `terminal` (Zoom won't recover on its own), the pop-up `dialogs` on
    screen (desktop path only), and `covers_canvas` when Zoom is up while
    the active source isn't a Zoom source - i.e. it's sitting on top of
    the browser (this exact situation went unnoticed for a day: the Zoom
    panel only shows for Zoom sources). Branches on ZOOM_JOIN_VIA to read
    either the desktop client (AT-SPI) or Zoom's web client (CDP/DOM) -
    see _zoom_meeting_status_client/_web - so every caller sees one
    shape regardless of path. Explicitly non-authoritative either way."""
    env = read_current_source()
    source_type = env.get("SOURCE_TYPE", "")
    join_via = env.get("ZOOM_JOIN_VIA", "client")
    if source_type == "zoom" and join_via == "web":
        data = _zoom_meeting_status_web()
    else:
        data = _zoom_meeting_status_client()
    data.setdefault("authoritative", False)
    data.setdefault("dialogs", [])
    data.setdefault("terminal", False)
    data["source_type"] = source_type
    data["join_via"] = join_via if source_type == "zoom" else None
    zoom_running = data["status"] != "not_joined" or data["service"] in (PHASE_LIVE, PHASE_STARTING)
    data["covers_canvas"] = bool(zoom_running and source_type and source_type != "zoom"
                                 and not (data["status"] == "not_joined" and data["service"] == PHASE_STOPPED))
    return data


def zoom_dialog(action: str) -> dict:
    """list: the Zoom pop-up dialogs on screen; dismiss: close them via
    their own OK/Close button (AT-SPI action), Escape, then WM close -
    see scripts/zoom-dialog.py for the safety rules on which buttons it
    will and won't press. Desktop-client path only - the web client has
    no OS-level dialogs to dismiss this way; use zoom_reset() there."""
    if action not in ("list", "dismiss"):
        raise ControlError(f"invalid dialog action: {action}")
    if read_current_source().get("ZOOM_JOIN_VIA") == "web":
        return {"dialogs": [], "dismissed": [], "remaining": [],
                "note": "web-client join - no desktop dialogs to check; use Reset Zoom instead"}
    argv = [SUDO, "-u", config.ZOOMBOT_USER, PYTHON3_BIN, str(ZOOM_DIALOG_SCRIPT), action]
    proc = run_as_zoombot(argv, timeout=20)
    try:
        return json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ControlError("zoom-dialog produced no result: " + proc.stderr.decode(errors="replace")[-300:])


ZOOM_JOIN_VERBS = {"join": "start", "leave": "stop", "rejoin": "restart"}


def zoom_join(action: str, source: dict | None) -> dict:
    """join / rejoin / leave for the ACTIVE Zoom source, via whichever
    producer its join_method resolves to (desktop client, or Zoom's web
    client on the browser-source kiosk). join and rejoin first re-apply
    the source's config (link, passcode, bot name, join policy) - that
    also stamps a fresh ZOOM_JOIN_EPOCH so the rejoin counter (and, for
    join_method=auto, the client-vs-web choice) starts fresh for this
    operator-initiated join. leave is a plain unit stop (a clean leave)
    of whichever producer is actually running right now."""
    verb = ZOOM_JOIN_VERBS.get(action)
    if not verb:
        raise ControlError("action must be join, rejoin or leave")
    if action in ("join", "rejoin"):
        if not source or source["type"] != "zoom":
            raise ControlError("The active source isn't a Zoom meeting - pick one first")
        from . import sources as sources_mod
        missing = sources_mod.zoom_missing(source)
        if missing:
            raise ControlError("Can't join yet - missing " + "; ".join(missing))
        apply_source_config(source)
        unit = producer_unit_for(source)
        other = "browser-source" if unit == "zoom" else "zoom"
        result = unit_action(unit, verb)
        try:
            unit_action(other, "stop")  # in case a previous join used the other path
        except ControlError:
            pass
        result["action"] = action
        result["join_via"] = "web" if unit == "browser-source" else "client"
        return result
    # leave: whichever producer the active zoom source is actually using
    # right now - never touches browser-source when the active source
    # isn't even a zoom one (that would be a live webpage source).
    env = read_current_source()
    if env.get("SOURCE_TYPE") != "zoom":
        raise ControlError("The active source isn't a Zoom meeting")
    unit = "browser-source" if env.get("ZOOM_JOIN_VIA") == "web" else "zoom"
    result = unit_action(unit, verb)
    result["action"] = action
    return result


def _zoom_reset_web() -> dict:
    """The web-client equivalent of "Reset Zoom": close tab, relaunch -
    zoom_web.reset() navigates the kiosk tab away and back to the join
    URL; if that's not even reachable (DevTools down), fall back to a
    full browser-source restart, same as any other "kiosk stuck" case."""
    from . import sources as sources_mod, zoom_web
    out: dict = {"dismissed": [], "remaining": [], "stopped": False}
    active = sources_mod.get_active_source()
    join_url = sources_mod.effective_zoom_join_url(active) if active else None
    if not join_url:
        out["results"] = [unit_action("browser-source", "restart")]
        out["stopped"] = True
        return out
    r = zoom_web.reset(join_url)
    if not r.get("ok"):
        out["dialog_error"] = r.get("error")
        out["results"] = [unit_action("browser-source", "restart")]
        out["stopped"] = True
        return out
    out["status_after_dismiss"] = zoom_meeting_status().get("status")
    return out


def zoom_reset() -> dict:
    """"Reset Zoom window": dismiss whatever dialogs Zoom has up (their
    own OK/Close buttons via AT-SPI), then look again; if a dialog is
    still stuck or Zoom is in a state it won't recover from, stop
    zoom.service and put the slate on :99. For a web-client join, this is
    "close tab, relaunch" instead (_zoom_reset_web). Never touches ffmpeg."""
    if read_current_source().get("ZOOM_JOIN_VIA") == "web":
        return _zoom_reset_web()
    out: dict = {"dismissed": [], "remaining": [], "stopped": False}
    try:
        d = zoom_dialog("dismiss")
        out["dismissed"] = d.get("dismissed", [])
        out["remaining"] = d.get("remaining", [])
    except ControlError as exc:
        out["dialog_error"] = str(exc)
    status = zoom_meeting_status()
    out["status_after_dismiss"] = status.get("status")
    if out["remaining"] or status.get("terminal") or status.get("status") in ("unknown",):
        out["results"] = zoom_leave("slate")
        out["stopped"] = True
    return out


def _want_state(desired_on: bool, kind: str) -> str:
    if kind == "mic":
        return "unmuted" if desired_on else "muted"
    return "on" if desired_on else "off"


def zoom_apply_join_options(source: dict) -> dict:
    """Bring Zoom's mic / camera / view in line with the meeting's saved
    options - the Linux deep link can't express them, so this happens
    after the join, using Zoom's own shortcuts and reading the result
    back from its accessibility tree. A control is toggled only when the
    read-back state differs from what's wanted; if the state can't be
    read at all it is left alone and reported as unknown (never blindly
    toggled: that could unmute a bot into a live meeting)."""
    options = source.get("options") or {}
    report: dict = {"applied": [], "skipped": [], "mic": None, "camera": None, "view": None}
    if read_current_source().get("ZOOM_JOIN_VIA") == "web":
        report["reason"] = "mic/camera/view aren't automated for the web-client join path"
        report["skipped"] = ["mic", "camera", "view"]
        return report
    status = zoom_meeting_status()
    report["status"] = status.get("status")
    if status.get("status") != "in_meeting":
        report["reason"] = "not in a meeting yet"
        return report
    for kind, opt in (("mic", "audio_on"), ("camera", "video_on")):
        want = _want_state(bool(options.get(opt, False)), kind)
        before = _zoom_atspi(kind)
        entry = {"want": want, "before": before.get("state"), "after": before.get("state"), "available": before.get("available", False)}
        if not before.get("available") or before.get("state") in ("unknown", "no_audio"):
            entry["note"] = before.get("reason") or ("no audio joined" if before.get("state") == "no_audio" else "state unreadable")
            report["skipped"].append(kind)
        elif before.get("state") != want:
            r = zoom_shortcut(kind)
            entry["after"] = (r.get("verify") or {}).get("state")
            report["applied"].append(kind)
        report[kind] = entry
    view = options.get("view", "speaker")
    try:
        zoom_shortcut("view-speaker" if view == "speaker" else "view-gallery")
        report["view"] = {"want": view, "sent": True, "note": "view mode has no readable state"}
        report["applied"].append("view")
    except ControlError as exc:
        report["view"] = {"want": view, "sent": False, "note": str(exc)}
    return report


def zoom_quit_to_slate() -> list[dict]:
    """"Reset Zoom window": leave whatever Zoom is doing (stop
    zoom.service - a clean leave) and put the plain slate on :99 so the
    canvas is calm. Never touches ffmpeg."""
    return zoom_leave("slate")


# --- window focus / VNC rate (interactive preview support) ---------------

FOCUS_WINDOW_SCRIPT = config.STREAM_SCRIPTS_DIR / "focus-window.sh"
VNC_RATE_SCRIPT = config.STREAM_SCRIPTS_DIR / "set-vnc-rate.sh"
OPEN_BROWSER_SCRIPT = config.STREAM_SCRIPTS_DIR / "open-browser.sh"
BROWSER_HOME_URL = "https://www.google.com/"
FOCUS_TARGETS = {"zoom", "browser"}
VNC_RATES = {"fast", "slow"}


def focus_window(which: str) -> dict:
    """Raise + focus the Zoom or browser window on :99 so subsequent input
    from the interactive preview lands where the operator expects."""
    if which not in FOCUS_TARGETS:
        raise ControlError(f"invalid focus target: {which}")
    proc = run_as_zoombot([SUDO, "-u", config.ZOOMBOT_USER, str(FOCUS_WINDOW_SCRIPT), which], timeout=10)
    try:
        data = json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ControlError("focus-window produced no result")
    if not data.get("ok"):
        raise ControlError(data.get("error") or "could not focus that window")
    return data


def open_browser_window(url: str) -> dict:
    """Open `url` on :99 in the Chrome profile that holds the operator's
    Google session (scripts/open-browser.sh): handed to the running
    kiosk when one holds that profile, otherwise an ordinary Chrome
    window. The caller (main.api_browser_open) tries DevTools navigation
    first; this is the path for when no controllable kiosk exists."""
    if not url.startswith(("http://", "https://")):
        raise ControlError("only http(s) URLs can be opened")
    proc = run_as_zoombot([SUDO, "-u", config.ZOOMBOT_USER, str(OPEN_BROWSER_SCRIPT), url], timeout=20)
    try:
        data = json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ControlError("open-browser produced no result")
    if not data.get("ok"):
        raise ControlError(data.get("error") or "could not open the browser")
    return data


def set_vnc_rate(mode: str) -> dict:
    """x11vnc poll rate: fast while the preview is interactive, slow
    otherwise. Read back from x11vnc, not assumed."""
    if mode not in VNC_RATES:
        raise ControlError(f"invalid vnc rate: {mode}")
    proc = run_as_zoombot([SUDO, "-u", config.ZOOMBOT_USER, str(VNC_RATE_SCRIPT), mode], timeout=10)
    try:
        data = json.loads(proc.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ControlError("set-vnc-rate produced no result")
    if not data.get("ok"):
        raise ControlError(data.get("error") or "could not change the VNC rate")
    return data


# --- browser (DevTools) reachability diagnosis ---------------------------

def _kiosk_chrome_cmdline() -> str | None:
    """The main kiosk Chrome process's command line (Chrome rewrites its
    argv into one space-joined string, so this is a substring search),
    or None when no kiosk Chrome is running. /proc/<pid>/cmdline is
    world-readable here (no hidepid), so this needs no sudo."""
    import psutil
    for p in psutil.process_iter(["username", "cmdline", "name"]):
        try:
            if p.info["username"] != config.ZOOMBOT_USER or not p.info["cmdline"]:
                continue
            cmd = " ".join(p.info["cmdline"])
            if "/chrome" in cmd.split(" ", 1)[0] and "--type=" not in cmd and "--user-data-dir=" in cmd:
                return cmd
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def browser_diagnosis() -> dict:
    """Why the kiosk browser isn't reachable over DevTools, as something
    the operator can act on. Called only after cdp.probe() failed."""
    source_type = read_current_source().get("SOURCE_TYPE", "")
    if source_type != "webpage":
        return {"connected": False, "code": "source_not_webpage",
                "reason": f"The active source is a {source_type or 'unset'} source, so the kiosk browser isn't running.",
                "fix": "Switch to a web page source to use these controls."}
    try:
        unit = unit_show("browser-source")
    except ControlError as exc:
        return {"connected": False, "code": "unit_unknown", "reason": f"Couldn't query browser-source.service: {exc}", "fix": ""}
    phase = unit["phase"]
    if phase in (PHASE_STOPPED, PHASE_FAILED):
        last_err = unit_last_error("browser-source") if phase == PHASE_FAILED else None
        return {"connected": False, "code": "unit_down",
                "reason": f"browser-source.service is {phase.lower()}" + (f" (last error: {last_err})" if last_err else "") + ".",
                "fix": "Start the browser source (tap the web source tile again), then check its log if it fails again.",
                "can_restart": True}
    if phase in (PHASE_STARTING, PHASE_RECONNECTING) or (unit.get("uptime_seconds") or 0) < 40:
        return {"connected": False, "code": "starting",
                "reason": "Chrome is still starting - the DevTools port comes up a few seconds after launch.",
                "fix": "Wait a few seconds."}
    cmd = _kiosk_chrome_cmdline()
    if cmd is None:
        return {"connected": False, "code": "chrome_missing",
                "reason": "browser-source.service is running but no kiosk Chrome process exists.",
                "fix": "Restart the browser source.", "can_restart": True}
    if "--remote-debugging-port" not in cmd:
        return {"connected": False, "code": "stale_launch",
                "reason": "Chrome is running but was started without the DevTools port (by an older browser-source.sh), so it can't be controlled.",
                "fix": "Restart the browser source - it relaunches Chrome with the port. Viewers see the page reload for a few seconds.",
                "can_restart": True}
    return {"connected": False, "code": "port_closed",
            "reason": f"Chrome should be listening on 127.0.0.1:{config.CHROME_DEBUG_PORT} but isn't answering.",
            "fix": "Restart the browser source.", "can_restart": True}


def open_url_in_account_profile(profile_id: str, url: str) -> None:
    """Opens `url` in an ordinary (no DevTools) Chrome window on :99 using
    the given account profile - the registration-form flow. URL must
    already have passed url_security.validate_url()."""
    if not _PROFILE_ID_RE.match(profile_id):
        raise ControlError("invalid profile id")
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(ACCOUNT_SCRIPT), "open", profile_id, url]
    proc = run_as_zoombot(argv, timeout=20)
    if proc.returncode != 0:
        raise ControlError(proc.stderr.decode(errors="replace").strip() or "could not open the page")




def zoom_leave(then: str) -> list[dict]:
    """Leaves the Zoom meeting (stops zoom.service - a clean leave, Zoom
    itself handles hanging up). `then` decides what happens to the
    stream: "stop" also stops ffmpeg-stream; "slate" leaves ffmpeg-stream
    running and sets a plain solid-color background on :99 (via
    xsetroot) so viewers see a calm screen instead of whatever the
    desktop happened to show - not a branded graphic or text, which
    would need an image-compositing step this project doesn't otherwise
    have; swap the color/add an image by hand over noVNC if wanted."""
    if then not in ("stop", "slate"):
        raise ControlError(f"invalid 'then' value: {then}")
    results = [unit_action("zoom", "stop")]
    if then == "stop":
        results.append(unit_action("ffmpeg-stream", "stop"))
    else:
        argv = [SUDO, "-u", config.ZOOMBOT_USER, XSETROOT_BIN, "-solid", SLATE_COLOR]
        proc = run_as_zoombot(argv, timeout=10)
        results.append({"ok": proc.returncode == 0, "action": "slate", "stderr": proc.stderr.decode(errors="replace").strip()})
    return results
