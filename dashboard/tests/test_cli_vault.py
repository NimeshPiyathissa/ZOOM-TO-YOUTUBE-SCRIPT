"""Tests for the Part 0/Part 3 CLI surface added to app/cli.py: `vault
init/unlock/change-master-password/set-unlock-mode/reset`, the unified
`setup` first-run flow, and `migrate-secrets`. Exercised entirely through
the non-interactive (environment-variable) mode so these run unattended in
CI without a TTY - the interactive getpass path is the same underlying
_read_secret() helper, just without an env var set, and isn't re-tested
here beyond test_read_secret_rejects_short_env_value."""
from __future__ import annotations

import sys

import pytest

from app import cli, config, db, security, secret_store, settings_store


@pytest.fixture(autouse=True)
def _isolated_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_DIR", tmp_path / "etc-zoom-stream")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_FILE", tmp_path / "etc-zoom-stream" / "master.key")
    monkeypatch.setattr(config, "VAULT_LOCK_SENTINEL_FILE", tmp_path / "vault.locked")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "STREAM_ENV_FILE", env_file)
    db.init_db()

    from app import env_store as _env_store
    monkeypatch.setattr(_env_store, "read_parsed", lambda: {})

    # migrate_secrets.backup_plaintext() reads .env via `sudo -u zoombot
    # cat` (real zoombot-owned files aren't directly readable by the
    # dashboard process) - fake that call for tests that exercise
    # migrate-secrets end to end, same convention as test_migrate_secrets.py.
    def _fake_run_as_zoombot(argv, input_bytes=None, timeout=15):
        if argv and argv[-1] == str(env_file) and "cat" in argv[-2]:
            return type("P", (), {"returncode": 0, "stdout": env_file.read_bytes(), "stderr": b""})()
        return type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr("app.control.run_as_zoombot", _fake_run_as_zoombot)

    from app import crypto
    monkeypatch.setattr(crypto, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(crypto, "ARGON2_MEMORY_COST_KIB", 8192)
    monkeypatch.setattr(crypto, "ARGON2_PARALLELISM", 1)
    secret_store._key = None
    secret_store._secrets = None

    for var in (
        "ZOOMBOT_MASTER_PASSWORD", "ZOOMBOT_OLD_MASTER_PASSWORD", "ZOOMBOT_UNLOCK_MODE",
        "ZOOMBOT_ADMIN_USERNAME", "ZOOMBOT_ADMIN_PASSWORD", "ZOOMBOT_VNC_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)

    yield
    secret_store._key = None
    secret_store._secrets = None


def _argv(*parts, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.cli", *parts])


# ------------------------------------------------------------------- vault

def test_vault_init_and_unlock_round_trip(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()
    assert secret_store.is_initialized()
    assert secret_store.is_unlocked()

    secret_store._key = None
    secret_store._secrets = None
    _argv("vault", "unlock", monkeypatch=monkeypatch)
    cli.cmd_vault()
    assert secret_store.is_unlocked()


def test_vault_init_refuses_if_already_initialized(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()

    _argv("vault", "init", monkeypatch=monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_vault()


def test_vault_cached_mode_without_root_is_refused(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "cached")
    monkeypatch.setattr(cli, "_is_root", lambda: False)
    _argv("vault", "init", monkeypatch=monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_vault()
    assert not secret_store.is_initialized()


def test_vault_cached_mode_with_root_succeeds(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "cached")
    monkeypatch.setattr(cli, "_is_root", lambda: True)
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()
    assert secret_store.is_initialized()


def test_vault_unlock_wrong_password_exits_nonzero(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "the real password")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()
    secret_store._key = None
    secret_store._secrets = None

    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "wrong password")
    _argv("vault", "unlock", monkeypatch=monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_vault()
    assert not secret_store.is_unlocked()


def test_vault_change_master_password(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "old password")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()

    monkeypatch.setenv("ZOOMBOT_OLD_MASTER_PASSWORD", "old password")
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "new password")
    _argv("vault", "change-master-password", monkeypatch=monkeypatch)
    cli.cmd_vault()

    secret_store._key = None
    secret_store._secrets = None
    secret_store.unlock("new password")
    assert secret_store.is_unlocked()


def test_vault_reset_requires_explicit_confirm(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "a reasonably long pw")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()

    _argv("vault", "reset", monkeypatch=monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_vault()
    assert secret_store.is_initialized()  # refused - still there

    _argv("vault", "reset", "--confirm", monkeypatch=monkeypatch)
    cli.cmd_vault()
    assert not secret_store.is_initialized()


def test_read_secret_rejects_short_env_value(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "short")
    with pytest.raises(SystemExit):
        cli._read_secret("Test secret", "SOME_VAR")


# ------------------------------------------------------------------- setup

def test_setup_creates_vault_and_admin_user_noninteractively(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ZOOMBOT_ADMIN_PASSWORD", "operator password 1")
    monkeypatch.setenv("ZOOMBOT_VNC_PASSWORD", "vnc password here")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")

    cli.cmd_setup()

    assert secret_store.is_unlocked()
    assert secret_store.get_secret("VNC_PASSWORD") == "vnc password here"

    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", ("operator",)).fetchone()
    assert row is not None
    assert security.verify_password("operator password 1", row["password_hash"])


def test_setup_refuses_if_vault_already_initialized(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ZOOMBOT_ADMIN_PASSWORD", "operator password 1")
    monkeypatch.setenv("ZOOMBOT_VNC_PASSWORD", "vnc password here")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    cli.cmd_setup()

    with pytest.raises(SystemExit):
        cli.cmd_setup()


# ---------------------------------------------------------- migrate-secrets

def test_migrate_secrets_requires_unlocked_vault(monkeypatch):
    _argv("migrate-secrets", "--dry-run", monkeypatch=monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_migrate_secrets()


def test_migrate_secrets_auto_unlocks_in_cached_mode_without_reentering_password(monkeypatch):
    """The entire point of cached unlock mode: a fresh process (a separate
    CLI invocation, exactly like the operator running migrate-secrets in a
    new shell after vault init) must not need the master password again."""
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "cached")
    monkeypatch.setattr(cli, "_is_root", lambda: True)
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()

    # simulate a fresh process: drop in-memory state, nothing re-entered
    secret_store._key = None
    secret_store._secrets = None

    from app import env_store
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "ytkey"})
    _argv("migrate-secrets", "--dry-run", monkeypatch=monkeypatch)
    cli.cmd_migrate_secrets()  # must not raise SystemExit / demand a password
    assert secret_store.is_unlocked()


def test_migrate_secrets_dry_run_then_real(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "a reasonably long pw")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()

    from app import env_store
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"YT_STREAM_KEY": "ytkey"})

    _argv("migrate-secrets", "--dry-run", monkeypatch=monkeypatch)
    cli.cmd_migrate_secrets()
    assert secret_store.get_all() == {}  # dry run changed nothing

    _argv("migrate-secrets", monkeypatch=monkeypatch)
    cli.cmd_migrate_secrets()
    assert secret_store.get_secret("YT_STREAM_KEY") == "ytkey"


def test_migrate_secrets_redact_flag_blanks_settings_json(monkeypatch):
    monkeypatch.setenv("ZOOMBOT_MASTER_PASSWORD", "a reasonably long pw")
    monkeypatch.setenv("ZOOMBOT_UNLOCK_MODE", "prompt")
    _argv("vault", "init", monkeypatch=monkeypatch)
    cli.cmd_vault()

    settings_store.save_settings({"telegram_bot_token": "secret-token"})

    _argv("migrate-secrets", "--redact", monkeypatch=monkeypatch)
    cli.cmd_migrate_secrets()

    assert secret_store.get_secret("telegram_bot_token") == "secret-token"
    assert settings_store.load_settings()["telegram_bot_token"] == ""
