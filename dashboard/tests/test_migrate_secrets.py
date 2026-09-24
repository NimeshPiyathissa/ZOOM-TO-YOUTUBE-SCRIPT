"""Tests for app/migrate_secrets.py: discovering every plaintext secret
this project has ever stored (.env, settings.json, sources/profiles DB
rows), backing them up, moving them into the vault, and - as a distinct,
later step - redacting the plaintext originals. Everything here runs
against fixtures in a scratch directory; nothing touches a real .env,
settings.json, or database (see the approved plan: this phase proves
correctness against synthetic data, Phase A2 is what runs it for real)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config, db, env_store, migrate_secrets, secret_store, settings_store


@pytest.fixture(autouse=True)
def _isolated_everything(tmp_path, monkeypatch):
    # Vault
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_DIR", tmp_path / "etc-zoom-stream")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_FILE", tmp_path / "etc-zoom-stream" / "master.key")
    monkeypatch.setattr(config, "VAULT_LOCK_SENTINEL_FILE", tmp_path / "vault.locked")
    from app import crypto
    monkeypatch.setattr(crypto, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(crypto, "ARGON2_MEMORY_COST_KIB", 8192)
    monkeypatch.setattr(crypto, "ARGON2_PARALLELISM", 1)
    secret_store._key = None
    secret_store._secrets = None

    # .env: on the real box this is zoombot-owned and unreadable by the
    # dashboard process directly - backup_plaintext() reads it via `sudo -u
    # zoombot cat`, same as env_store.read_parsed(). Here that's a fake
    # run_as_zoombot that just cats the real scratch file's bytes, so the
    # backup path is exercised the same way in tests as in production
    # (found a real PermissionError-on-direct-copy bug this way once already).
    env_file = tmp_path / ".env"
    env_file.write_text('ZOOM_LINK="https://zoom.us/j/123?pwd=abc123"\nYT_STREAM_KEY="ytkey123"\n', encoding="utf-8")
    monkeypatch.setattr(config, "STREAM_ENV_FILE", env_file)

    monkeypatch.setattr("app.control.run_as_zoombot", _fake_run_as_zoombot(env_file))

    # settings.json (real file - settings_store does direct file I/O, no sudo)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    settings_store.save_settings({
        "telegram_bot_token": "123456:AA-bot-token",
        "telegram_api_hash": "abcdef0123456789",
        "telegram_chat_id": "-100123456",  # not secret - stays
    })

    # DB (sources + profiles)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "dashboard.db")
    db.init_db()

    yield
    secret_store._key = None
    secret_store._secrets = None


def _fake_run_as_zoombot(env_file, calls=None):
    """A run_as_zoombot stand-in that answers the `cat .env` read (for
    backup_plaintext()) with the real scratch .env's bytes, and treats
    everything else (write-env.sh writes) as a successful no-op, recording
    the piped input into `calls` if given - covers both call shapes a
    single test may trigger (backup's read, then a later write)."""
    def _run(argv, input_bytes=None, timeout=15):
        if argv and argv[-1] == str(env_file) and "cat" in argv[-2]:
            return type("P", (), {"returncode": 0, "stdout": env_file.read_bytes(), "stderr": b""})()
        if calls is not None:
            calls.append(input_bytes)
        return type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
    return _run


def _seed_source(url, options, type_="zoom"):
    now = 0.0
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sources (name, type, url, options, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (f"src-{url}", type_, url, json.dumps(options), now, now),
        )
        return cur.lastrowid


def _seed_profile(zoom_link, zoom_passcode):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO profiles (name, zoom_link, zoom_passcode, bot_name, created_at, updated_at) "
            "VALUES (?,?,?,?,0,0)",
            ("legacy profile", zoom_link, zoom_passcode, "Stream Bot"),
        )
        return cur.lastrowid


# --------------------------------------------------------------- discovery

def test_discover_env_secrets_only_returns_secret_keys(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {
        "ZOOM_LINK": "https://zoom.us/j/123?pwd=abc", "BOT_NAME": "Stream Bot",
        "YT_STREAM_KEY": "ytkey", "RESOLUTION": "1920x1080",
    })
    found = migrate_secrets.discover_env_secrets()
    assert found == {"ZOOM_LINK": "https://zoom.us/j/123?pwd=abc", "YT_STREAM_KEY": "ytkey"}


def test_discover_settings_secrets_only_returns_secret_keys():
    found = migrate_secrets.discover_settings_secrets()
    assert found == {
        "telegram_bot_token": "123456:AA-bot-token",
        "telegram_api_hash": "abcdef0123456789",
    }
    # the non-secret chat_id must never show up here
    assert "telegram_chat_id" not in found


def test_discover_source_secrets_extracts_passcode_and_url_tokens():
    sid = _seed_source("https://zoom.us/j/999?pwd=linkpwd", {"passcode": "optionspwd", "join_url": "https://zoom.us/w/999?tk=abc.def"})
    found = migrate_secrets.discover_source_secrets()
    assert found[migrate_secrets.SOURCE_SECRET_KEY(sid, "passcode")] == "optionspwd"
    assert found[migrate_secrets.SOURCE_SECRET_KEY(sid, "url_pwd")] == "linkpwd"
    assert found[migrate_secrets.SOURCE_SECRET_KEY(sid, "join_url_tk")] == "abc.def"


def test_discover_source_secrets_ignores_non_zoom_sources():
    _seed_source("https://example.com/live.m3u8", {}, type_="direct")
    found = migrate_secrets.discover_source_secrets()
    assert found == {}


def test_discover_profile_secrets_extracts_passcode_and_link_tokens():
    pid = _seed_profile("https://zoom.us/j/777?pwd=legacypwd", "legacypasscode")
    found = migrate_secrets.discover_profile_secrets()
    assert found[migrate_secrets.PROFILE_SECRET_KEY(pid, "passcode")] == "legacypasscode"
    assert found[migrate_secrets.PROFILE_SECRET_KEY(pid, "url_pwd")] == "legacypwd"


def test_discover_profile_secrets_tolerates_missing_table(monkeypatch):
    with db.get_conn() as conn:
        conn.execute("DROP TABLE profiles")
    assert migrate_secrets.discover_profile_secrets() == {}


# ----------------------------------------------------------------- backup

def test_backup_plaintext_copies_existing_sources_only():
    import pathlib

    written = migrate_secrets.backup_plaintext()

    assert set(written.keys()) == {"env", "settings", "db"}
    for path in written.values():
        assert pathlib.Path(path).is_file()
    assert pathlib.Path(written["env"]).read_text(encoding="utf-8") == config.STREAM_ENV_FILE.read_text(encoding="utf-8")


def test_backup_plaintext_skips_missing_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "does-not-exist.json")
    written = migrate_secrets.backup_plaintext()
    assert "settings" not in written
    assert "env" in written and "db" in written


# ------------------------------------------------------------------- run()

def test_run_dry_run_finds_everything_and_changes_nothing(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "ytkey"})
    _seed_source("https://zoom.us/j/999?pwd=linkpwd", {"passcode": "optionspwd"})

    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")
    report = migrate_secrets.run(dry_run=True)

    assert report["dry_run"] is True
    assert "YT_STREAM_KEY" in report["keys_found"]
    assert "telegram_bot_token" in report["keys_found"]
    assert report["backup_paths"] == {}
    assert report["applied_to_vault"] is False
    assert secret_store.get_all() == {}  # vault untouched


def test_run_real_backs_up_and_applies_to_vault(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "ytkey"})
    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")

    report = migrate_secrets.run(dry_run=False)

    assert report["applied_to_vault"] is True
    assert set(report["backup_paths"].keys()) == {"env", "settings", "db"}
    assert secret_store.get_secret("YT_STREAM_KEY") == "ytkey"
    assert secret_store.get_secret("telegram_bot_token") == "123456:AA-bot-token"


# --------------------------------------------------------------- redaction

def test_redact_env_secrets_blanks_only_secret_keys(monkeypatch):
    calls = []

    class _FakeProc:
        returncode = 0
        stderr = b""

    def _fake_run(argv, input_bytes=None, timeout=15):
        calls.append(input_bytes)
        return _FakeProc()

    monkeypatch.setattr(env_store, "read_parsed", lambda: {
        "ZOOM_LINK": "https://zoom.us/j/123?pwd=abc", "BOT_NAME": "Stream Bot", "YT_STREAM_KEY": "ytkey",
    })
    monkeypatch.setattr("app.control.run_as_zoombot", _fake_run)

    migrate_secrets.redact_env_secrets()

    assert len(calls) == 1
    content = calls[0]
    assert b"ZOOM_LINK\x00\x00" in content  # blanked
    assert b"YT_STREAM_KEY\x00\x00" in content  # blanked
    assert b"BOT_NAME\x00Stream Bot\x00" in content  # untouched


def test_redact_settings_secrets_blanks_only_secret_keys():
    migrate_secrets.redact_settings_secrets()
    settings = settings_store.load_settings()
    assert settings["telegram_bot_token"] == ""
    assert settings["telegram_api_hash"] == ""
    assert settings["telegram_chat_id"] == "-100123456"  # non-secret, untouched


def test_redact_source_and_profile_secrets_blanks_passcode_and_url_tokens():
    sid = _seed_source("https://zoom.us/j/999?pwd=linkpwd", {"passcode": "optionspwd", "join_url": "https://zoom.us/w/999?tk=abc.def"})
    pid = _seed_profile("https://zoom.us/j/777?pwd=legacypwd", "legacypasscode")

    migrate_secrets.redact_source_and_profile_secrets()

    with db.get_conn() as conn:
        src = dict(conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())
        prof = dict(conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone())

    options = json.loads(src["options"])
    assert options["passcode"] == ""
    assert "tk=abc.def" not in options["join_url"]
    assert "pwd=linkpwd" not in src["url"]
    assert prof["zoom_passcode"] == ""
    assert "pwd=legacypwd" not in prof["zoom_link"]


def test_rollback_restores_env_settings_and_db_from_backup(monkeypatch, tmp_path):
    env_file = config.STREAM_ENV_FILE
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"ZOOM_LINK": "https://zoom.us/j/123?pwd=abc123", "YT_STREAM_KEY": "ytkey123"})
    monkeypatch.setattr("app.control.run_as_zoombot", _fake_run_as_zoombot(env_file))

    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")
    report = migrate_secrets.run(dry_run=False)
    settings_before = settings_store.load_settings()
    assert settings_before["telegram_bot_token"] == "123456:AA-bot-token"

    migrate_secrets.redact_all_plaintext()
    assert settings_store.load_settings()["telegram_bot_token"] == ""

    # capture what write-env.sh would have received for the rollback, so
    # we can assert it's the ORIGINAL .env content, not the redacted one
    calls = []
    monkeypatch.setattr("app.control.run_as_zoombot", _fake_run_as_zoombot(env_file, calls))

    migrate_secrets.rollback_from_backup(report["backup_paths"])

    assert settings_store.load_settings()["telegram_bot_token"] == "123456:AA-bot-token"  # restored
    assert len(calls) == 1
    original_env_bytes = Path(report["backup_paths"]["env"]).read_bytes()
    assert calls[0] == original_env_bytes


def test_full_migrate_then_redact_round_trip_preserves_secrets_in_vault(monkeypatch):
    """The end-to-end story the plan requires: after discover -> backup ->
    apply -> redact, nothing plaintext remains, but every value still
    round-trips correctly out of the vault."""
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "ytkey", "BOT_NAME": "Stream Bot"})
    monkeypatch.setattr("app.control.run_as_zoombot", _fake_run_as_zoombot(config.STREAM_ENV_FILE))

    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")
    report = migrate_secrets.run(dry_run=False)
    migrate_secrets.redact_all_plaintext()

    assert secret_store.get_secret("YT_STREAM_KEY") == "ytkey"  # still there, from the vault
    settings = settings_store.load_settings()
    assert settings["telegram_bot_token"] == ""  # plaintext original gone
    assert report["backup_paths"]  # rollback copy exists
