"""One-time migration of every plaintext secret this project has ever
stored into the encrypted vault (Part 0). Covers, per this session's
field-by-field audit of the actual code (not a guess):

  .env (via env_store)      - config.SECRET_ENV_KEYS: ZOOM_LINK (may embed
                               a pwd= param), ZOOM_PASSCODE, YT_STREAM_KEY,
                               VNC_PASSWORD, TELEGRAM_BOT_TOKEN
  settings.json (via        - config.SETTINGS_SECRET_KEYS: telegram_bot_token,
    settings_store)           telegram_api_hash, telegram_session_string,
                               vnc_password (a second, independently-drifting
                               copy of the .env value - see vncauth.py)
  sources table (options     - each row's "passcode", plus any pwd=/tk=
    JSON + url column)         embedded in "url"/"join_url"
  profiles table (legacy,    - zoom_passcode, plus any pwd= embedded in
    superseded by sources)     zoom_link

Every discovered value is namespaced by where it came from when written
into the vault (e.g. "source:3:passcode"), because the vault is a single
flat dict[str, str] and per-row secrets need a stable, collision-free key -
see SOURCE_SECRET_KEY()/PROFILE_SECRET_KEY() below. .env/settings.json keys
keep their existing names unchanged (e.g. "YT_STREAM_KEY") since callers
already know those names.

This module only ever touches config.*-referenced paths (monkeypatchable
in tests, same convention as test_secret_store.py) and a sqlite connection
via app.db - never a hardcoded path. discover()/backup() are read-only;
apply_to_vault() writes only to the vault; redact_plaintext() is the only
function that modifies the original plaintext sources, and is always a
separate, explicit call - never fused into the same step as discovering or
vaulting a secret, so a caller (the CLI, or Phase A2's real run) can stop
and verify between them.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from . import config, db, env_store, secret_store, settings_store

PWD_PARAM_RE = re.compile(r"[?&]pwd=([^&]+)")
TK_PARAM_RE = re.compile(r"[?&]tk=([^&]+)")


def SOURCE_SECRET_KEY(source_id: int, field: str) -> str:
    return f"source:{source_id}:{field}"


def PROFILE_SECRET_KEY(profile_id: int, field: str) -> str:
    return f"profile:{profile_id}:{field}"


# ------------------------------------------------------------- discovery

def discover_env_secrets() -> dict[str, str]:
    current = env_store.read_parsed()
    return {k: current[k] for k in config.SECRET_ENV_KEYS if current.get(k)}


def discover_settings_secrets() -> dict[str, str]:
    settings = settings_store.load_settings()
    return {k: str(settings[k]) for k in config.SETTINGS_SECRET_KEYS if settings.get(k)}


def _url_tokens(url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not url:
        return out
    m = PWD_PARAM_RE.search(url)
    if m:
        out["url_pwd"] = m.group(1)
    m = TK_PARAM_RE.search(url)
    if m:
        out["url_tk"] = m.group(1)
    return out


def discover_source_secrets() -> dict[str, str]:
    """Zoom-type sources only - webpage/direct sources have no secret
    fields (their URLs go through url_security.py, not this vault)."""
    out: dict[str, str] = {}
    with db.get_conn() as conn:
        try:
            rows = conn.execute("SELECT id, type, url, options FROM sources").fetchall()
        except sqlite3.OperationalError:
            return out  # table doesn't exist yet (db.init_db() never ran on this install)
    for row in rows:
        if row["type"] != "zoom":
            continue
        sid = row["id"]
        try:
            options = json.loads(row["options"] or "{}")
        except (TypeError, ValueError):
            options = {}
        passcode = str(options.get("passcode", "") or "")
        if passcode:
            out[SOURCE_SECRET_KEY(sid, "passcode")] = passcode
        join_url = str(options.get("join_url", "") or "")
        for suffix, val in _url_tokens(join_url).items():
            out[SOURCE_SECRET_KEY(sid, f"join_{suffix}")] = val
        for suffix, val in _url_tokens(row["url"] or "").items():
            out[SOURCE_SECRET_KEY(sid, suffix)] = val
    return out


def discover_profile_secrets() -> dict[str, str]:
    """Legacy `profiles` table (superseded by `sources`, see cli.py's
    migrate-sources) - still migrated so nothing is lost on an install
    that never ran that migration."""
    out: dict[str, str] = {}
    with db.get_conn() as conn:
        try:
            rows = conn.execute("SELECT id, zoom_link, zoom_passcode FROM profiles").fetchall()
        except sqlite3.OperationalError:
            return out  # table doesn't exist on a fresh/already-cleaned install
    for row in rows:
        pid = row["id"]
        passcode = str(row["zoom_passcode"] or "")
        if passcode:
            out[PROFILE_SECRET_KEY(pid, "passcode")] = passcode
        for suffix, val in _url_tokens(row["zoom_link"] or "").items():
            out[PROFILE_SECRET_KEY(pid, suffix)] = val
    return out


def discover_all() -> dict[str, str]:
    merged: dict[str, str] = {}
    merged.update(discover_env_secrets())
    merged.update(discover_settings_secrets())
    merged.update(discover_source_secrets())
    merged.update(discover_profile_secrets())
    return merged


# ------------------------------------------------------------------ backup

def backup_plaintext() -> dict[str, str]:
    """Timestamped, chmod-600 copies of every plaintext source, made
    before any vault or redaction step. Never overwritten, never
    auto-deleted - this is the rollback path. Returns the paths actually
    written (a source that doesn't exist on this install is skipped, not
    an error).

    .env lives under the zoombot user (chmod 600, not readable by the
    dashboard process directly) - this process reads it the same way
    env_store.read_parsed() does, via the narrow `sudo -u zoombot cat`
    sudoers rule, rather than a direct file open which would just raise
    PermissionError (found during the real Phase A2 migration run - a
    direct shutil.copy2 on STREAM_ENV_FILE fails for exactly this reason).
    The backup itself is written under DATA_DIR (dashboard-owned) instead
    of alongside the original in zoombot's directory, since this process
    has no write access there either."""
    from .control import ControlError, SUDO, run_as_zoombot
    from .env_store import CAT

    ts = time.strftime("%Y%m%d-%H%M%S")
    written: dict[str, str] = {}

    argv = [SUDO, "-u", config.ZOOMBOT_USER, CAT, str(config.STREAM_ENV_FILE)]
    proc = run_as_zoombot(argv)
    if proc.returncode == 0:
        dest = config.DATA_DIR / f".env.pre-vault-backup.{ts}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(proc.stdout)
        dest.chmod(0o600)
        written["env"] = str(dest)
    elif b"No such file or directory" not in proc.stderr:
        # Unlike settings.json/the db (legitimately absent on some
        # installs), .env not being readable is never a benign "doesn't
        # exist on this install" case - lib.sh itself refuses to run
        # without it. Fail loudly rather than silently proceed to migrate
        # secrets with no backup of the thing they came from.
        raise ControlError(
            "could not back up .env (sudo -u zoombot cat failed): " + proc.stderr.decode(errors="replace")
        )

    if config.SETTINGS_FILE.exists():
        dest = config.SETTINGS_FILE.with_name(f"settings.json.pre-vault-backup.{ts}")
        shutil.copy2(config.SETTINGS_FILE, dest)
        dest.chmod(0o600)
        written["settings"] = str(dest)

    if Path(config.DB_PATH).exists():
        dest = Path(config.DB_PATH).with_name(f"dashboard.db.pre-vault-backup.{ts}")
        shutil.copy2(config.DB_PATH, dest)
        dest.chmod(0o600)
        written["db"] = str(dest)

    return written


# ------------------------------------------------------------- vault apply

def apply_to_vault(secrets: dict[str, str]) -> None:
    """Merges the discovered secrets into an already-initialized, unlocked
    vault. Does not initialize the vault itself - that's a separate,
    deliberate step (`vault init`) so the master password / unlock mode
    choice is never implicit in a migration run."""
    secret_store.set_secrets(secrets)


# --------------------------------------------------------------- redaction

def redact_env_secrets() -> None:
    """Blanks config.SECRET_ENV_KEYS in .env, leaving every non-secret key
    untouched. Bypasses env_store.write_updates()'s form-validation layer
    (built for partial, user-facing edits, not a full migration blank-out,
    and it explicitly excludes VNC_PASSWORD) and instead re-encodes the
    full ALL_ENV_KEYS set directly through the same trusted write-env.sh
    primitive env_store.py itself uses."""
    from .control import ControlError, SUDO, run_as_zoombot

    current = env_store.read_parsed()
    merged = {key: current.get(key, "") for key in config.ALL_ENV_KEYS}
    for key in config.SECRET_ENV_KEYS:
        merged[key] = ""
    content = env_store._encode_env_pairs(merged)
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.WRITE_ENV_SCRIPT)]
    proc = run_as_zoombot(argv, input_bytes=content, timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to redact .env: " + proc.stderr.decode(errors="replace"))


def redact_settings_secrets() -> None:
    settings_store.save_settings({k: "" for k in config.SETTINGS_SECRET_KEYS})


def redact_source_and_profile_secrets() -> None:
    """Blanks passcode/url tokens directly in the sources/profiles tables.
    Deliberately bypasses sources.py's higher-level update functions (they
    validate for the user-facing "edit a source" flow, not a bulk
    redaction) and writes SQL directly, mirroring how cli.py's existing
    migrate-sources already reaches into these tables directly for
    one-time migration work."""
    with db.get_conn() as conn:
        try:
            rows = conn.execute("SELECT id, type, url, options FROM sources").fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            if row["type"] != "zoom":
                continue
            try:
                options = json.loads(row["options"] or "{}")
            except (TypeError, ValueError):
                options = {}
            changed = False
            if options.get("passcode"):
                options["passcode"] = ""
                changed = True
            if options.get("join_url"):
                new_join = PWD_PARAM_RE.sub("pwd=", TK_PARAM_RE.sub("tk=", options["join_url"]))
                if new_join != options["join_url"]:
                    options["join_url"] = new_join
                    changed = True
            new_url = row["url"] or ""
            new_url = PWD_PARAM_RE.sub("pwd=", TK_PARAM_RE.sub("tk=", new_url))
            if changed or new_url != (row["url"] or ""):
                conn.execute(
                    "UPDATE sources SET options=?, url=?, updated_at=? WHERE id=?",
                    (json.dumps(options), new_url, time.time(), row["id"]),
                )

        try:
            prows = conn.execute("SELECT id, zoom_link, zoom_passcode FROM profiles").fetchall()
        except sqlite3.OperationalError:
            prows = []
        for row in prows:
            new_link = PWD_PARAM_RE.sub("pwd=", TK_PARAM_RE.sub("tk=", row["zoom_link"] or ""))
            if row["zoom_passcode"] or new_link != (row["zoom_link"] or ""):
                conn.execute(
                    "UPDATE profiles SET zoom_link=?, zoom_passcode=?, updated_at=? WHERE id=?",
                    (new_link, "", time.time(), row["id"]),
                )


def redact_all_plaintext() -> None:
    redact_env_secrets()
    redact_settings_secrets()
    redact_source_and_profile_secrets()


# --------------------------------------------------------------- rollback

def rollback_from_backup(backup_paths: dict[str, str]) -> None:
    """Restores .env/settings.json/dashboard.db from the timestamped
    backups backup_plaintext() (or run()'s report) produced - the
    documented recovery if a migration/redaction run needs to be undone.
    Copies straight over the current file via the same trusted write-env.sh
    primitive for .env (so permissions/ownership stay correct on the real
    box) and a direct file copy for settings.json/the sqlite db, which
    this process already owns outright. Does not touch the vault: leaving
    already-migrated values sitting in the encrypted store is harmless,
    and a stalled/failed vault write means nothing was migrated into it in
    the first place - either way, rollback is purely about restoring the
    plaintext files this process is allowed to write directly."""
    from .control import ControlError, SUDO, run_as_zoombot

    if "env" in backup_paths:
        content = Path(backup_paths["env"]).read_bytes()
        argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.WRITE_ENV_SCRIPT)]
        proc = run_as_zoombot(argv, input_bytes=content, timeout=15)
        if proc.returncode != 0:
            raise ControlError("failed to restore .env: " + proc.stderr.decode(errors="replace"))
    if "settings" in backup_paths:
        shutil.copy2(backup_paths["settings"], config.SETTINGS_FILE)
    if "db" in backup_paths:
        shutil.copy2(backup_paths["db"], config.DB_PATH)


# --------------------------------------------------------------- orchestration

def run(*, dry_run: bool) -> dict:
    """Discover -> backup -> (if not dry_run) apply to vault. Redaction of
    the plaintext originals is NOT part of this function - it's a
    separate, explicitly later step (redact_all_plaintext), so a caller
    can verify the vault round-trips correctly before anything plaintext
    is touched. Returns a report describing what would/did happen; never
    includes secret values, only key names and counts."""
    discovered = discover_all()
    report: dict = {
        "dry_run": dry_run,
        "keys_found": sorted(discovered.keys()),
        "count": len(discovered),
        "backup_paths": {},
        "applied_to_vault": False,
    }
    if dry_run:
        return report

    report["backup_paths"] = backup_plaintext()
    apply_to_vault(discovered)
    report["applied_to_vault"] = True
    return report
