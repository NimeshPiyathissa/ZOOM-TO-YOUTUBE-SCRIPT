"""Tests for app/crypto.py + app/secret_store.py (Part 0: the Master
Encryption Password and encrypted-at-rest secret store). Covers the
verification scenarios the productionization work explicitly calls for:
round-trip, a wrong-password test, a change-master-password test, and
both unlock modes (including the "reboot" case, simulated as a fresh
process by clearing the module's in-memory state between calls)."""
from __future__ import annotations

import importlib
import os

import pytest

from app import config, crypto, secret_store


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Points every path secret_store.py touches at a scratch directory,
    resets its module-level in-memory state before and after each test
    (so tests never share state or touch real install paths), and trades
    KDF strength for speed - the real ARGON2_* constants are covered
    separately by test_crypto_production_defaults_produce_a_valid_key,
    once, at full cost; every other test only needs the KDF to be
    correct, not slow."""
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_DIR", tmp_path / "etc-zoom-stream")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_FILE", tmp_path / "etc-zoom-stream" / "master.key")
    monkeypatch.setattr(config, "VAULT_LOCK_SENTINEL_FILE", tmp_path / "vault.locked")
    monkeypatch.setattr(crypto, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(crypto, "ARGON2_MEMORY_COST_KIB", 8192)
    monkeypatch.setattr(crypto, "ARGON2_PARALLELISM", 1)
    secret_store._key = None
    secret_store._secrets = None
    yield
    secret_store._key = None
    secret_store._secrets = None


# --------------------------------------------------------------- crypto.py

def test_crypto_round_trip():
    salt = crypto.new_salt()
    key = crypto.derive_key("correct horse battery staple", salt)
    ct = crypto.encrypt(key, b"the zoom passcode")
    assert crypto.decrypt(key, ct) == b"the zoom passcode"


def test_crypto_wrong_key_fails_closed():
    salt = crypto.new_salt()
    key1 = crypto.derive_key("password one", salt)
    key2 = crypto.derive_key("password two", salt)
    ct = crypto.encrypt(key1, b"secret")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(key2, ct)


def test_crypto_tampered_ciphertext_fails_closed():
    salt = crypto.new_salt()
    key = crypto.derive_key("pw", salt)
    ct = bytearray(crypto.encrypt(key, b"secret"))
    ct[-1] ^= 0xFF  # flip a bit in the auth tag
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(key, bytes(ct))


def test_crypto_same_password_different_salt_different_key():
    k1 = crypto.derive_key("same password", crypto.new_salt())
    k2 = crypto.derive_key("same password", crypto.new_salt())
    assert k1 != k2


def test_crypto_production_defaults_produce_a_valid_key(monkeypatch):
    """The one test in this file that runs the *real* production argon2id
    parameters (undoing the fixture's speed override) - confirms the
    documented defaults actually work end-to-end, at the cost of a few
    real seconds."""
    monkeypatch.undo()  # restore crypto.ARGON2_* to their real module values
    salt = crypto.new_salt()
    key = crypto.derive_key("a realistic master password", salt)
    assert len(key) == crypto.KEY_LEN


# ----------------------------------------------------------- secret_store

def test_not_initialized_by_default():
    assert secret_store.is_initialized() is False
    assert secret_store.read_meta() is None


def test_initialize_and_unlock_round_trip():
    secret_store.initialize(
        "correct horse battery staple",
        initial_secrets={"YT_STREAM_KEY": "abc123", "ZOOM_PASSCODE": "hunter2"},
        unlock_mode="prompt",
    )
    assert secret_store.is_unlocked() is True
    assert secret_store.get_all() == {"YT_STREAM_KEY": "abc123", "ZOOM_PASSCODE": "hunter2"}

    # simulate a fresh process: drop in-memory state, nothing cached (prompt mode)
    secret_store._key = None
    secret_store._secrets = None
    assert secret_store.is_unlocked() is False
    assert secret_store.try_auto_unlock() is False  # prompt mode never auto-unlocks

    secret_store.unlock("correct horse battery staple")
    assert secret_store.get_secret("YT_STREAM_KEY") == "abc123"


def test_wrong_password_fails_closed_and_does_not_unlock():
    secret_store.initialize("the real password", initial_secrets={"X": "y"}, unlock_mode="prompt")
    secret_store._key = None
    secret_store._secrets = None

    with pytest.raises(crypto.DecryptionError):
        secret_store.unlock("not the real password")
    assert secret_store.is_unlocked() is False
    with pytest.raises(secret_store.VaultLockedError):
        secret_store.get_all()


def test_locked_access_raises():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="prompt")
    secret_store.lock()
    assert secret_store.is_unlocked() is False
    with pytest.raises(secret_store.VaultLockedError):
        secret_store.get_secret("A")
    with pytest.raises(secret_store.VaultLockedError):
        secret_store.set_secrets({"A": "2"})


def test_set_secrets_persists_and_survives_relock():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="prompt")
    secret_store.set_secrets({"A": "2", "B": "new"})
    assert secret_store.get_all() == {"A": "2", "B": "new"}

    secret_store._key = None
    secret_store._secrets = None
    secret_store.unlock("pw")
    assert secret_store.get_all() == {"A": "2", "B": "new"}


def test_change_master_password_reencrypts_and_old_password_stops_working():
    secret_store.initialize("old password", initial_secrets={"A": "1"}, unlock_mode="prompt")
    secret_store.change_master_password("old password", "new password")
    assert secret_store.get_all() == {"A": "1"}  # data unchanged

    secret_store._key = None
    secret_store._secrets = None
    with pytest.raises(crypto.DecryptionError):
        secret_store.unlock("old password")
    assert secret_store.is_unlocked() is False

    secret_store.unlock("new password")
    assert secret_store.get_all() == {"A": "1"}


def test_change_master_password_wrong_old_password_rejected():
    secret_store.initialize("old password", initial_secrets={"A": "1"}, unlock_mode="prompt")
    with pytest.raises(crypto.DecryptionError):
        secret_store.change_master_password("wrong old password", "new password")
    # store must be untouched: original password still works
    secret_store._key = None
    secret_store._secrets = None
    secret_store.unlock("old password")
    assert secret_store.get_all() == {"A": "1"}


# ------------------------------------------------------- unlock mode: cached

def test_cached_mode_auto_unlocks_across_simulated_reboot():
    secret_store.initialize(
        "pw", initial_secrets={"VNC_PASSWORD": "vncpw"}, unlock_mode="cached",
    )
    assert config.MASTER_KEY_CACHE_FILE.exists()

    # simulate a reboot: fresh process state, but the cache file persists on disk
    secret_store._key = None
    secret_store._secrets = None
    assert secret_store.is_unlocked() is False

    assert secret_store.try_auto_unlock() is True
    assert secret_store.get_secret("VNC_PASSWORD") == "vncpw"


def test_cached_mode_manual_lock_blocks_auto_unlock_until_reunlocked():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="cached")
    secret_store.lock()
    assert secret_store.is_unlocked() is False
    # cache file is still on disk, but the manual-lock sentinel must block it
    assert config.MASTER_KEY_CACHE_FILE.exists()
    assert secret_store.try_auto_unlock() is False

    secret_store.unlock("pw")
    assert secret_store.is_unlocked() is True
    # sentinel cleared by a successful unlock -> auto-unlock works again next "reboot"
    secret_store._key = None
    secret_store._secrets = None
    assert secret_store.try_auto_unlock() is True


def test_switch_from_cached_to_prompt_removes_cache_file():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="cached")
    assert config.MASTER_KEY_CACHE_FILE.exists()
    secret_store.set_unlock_mode("prompt", password="pw")
    assert not config.MASTER_KEY_CACHE_FILE.exists()

    secret_store._key = None
    secret_store._secrets = None
    assert secret_store.try_auto_unlock() is False  # no more cache file, no more auto-unlock


def test_switch_from_prompt_to_cached_writes_cache_file_and_enables_auto_unlock():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="prompt")
    assert not config.MASTER_KEY_CACHE_FILE.exists()
    secret_store.set_unlock_mode("cached", password="pw")
    assert config.MASTER_KEY_CACHE_FILE.exists()

    secret_store._key = None
    secret_store._secrets = None
    assert secret_store.try_auto_unlock() is True


def test_change_master_password_rewrites_cache_file_in_cached_mode():
    secret_store.initialize("old pw", initial_secrets={"A": "1"}, unlock_mode="cached")
    secret_store.change_master_password("old pw", "new pw")

    # stale-cache scenario would be: old cache file, new password - make sure
    # the *current* cache file matches the *new* key (i.e. auto-unlock still works)
    secret_store._key = None
    secret_store._secrets = None
    assert secret_store.try_auto_unlock() is True
    assert secret_store.get_all() == {"A": "1"}


# --------------------------------------------------------------- reset_vault

def test_reset_vault_wipes_store_and_cache_and_sentinel():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="cached")
    secret_store.lock()
    secret_store.reset_vault()
    assert secret_store.is_initialized() is False
    assert not config.MASTER_KEY_CACHE_FILE.exists()
    assert not config.VAULT_LOCK_SENTINEL_FILE.exists()
    assert secret_store.is_unlocked() is False


def test_double_initialize_refused():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="prompt")
    with pytest.raises(secret_store.VaultAlreadyInitializedError):
        secret_store.initialize("pw2", initial_secrets={"B": "2"}, unlock_mode="prompt")


def test_write_raw_chowns_store_file_to_dashboard_user_when_run_as_root(monkeypatch):
    """Regression test for a real bug found during the Phase A2 live
    migration: running `vault init` via sudo (required for cached mode's
    root-owned key cache file) left secrets.enc.json itself root:root -
    unreadable by the actual `dashboard` service account that needs to
    read/write it on every ordinary unlock/set_secrets call afterward."""
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)), raising=False)

    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="prompt")

    assert len(calls) >= 1  # at least the initial write
    path, uid, gid = calls[-1]
    assert path.endswith("secrets.enc.json") or "secrets" in path
    import grp
    import pwd
    assert uid == pwd.getpwnam(config.DASHBOARD_USER).pw_uid
    assert gid == grp.getgrnam(config.DASHBOARD_GROUP).gr_gid


def test_write_raw_skips_chown_when_not_root(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)), raising=False)

    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="prompt")

    assert calls == []


def test_read_meta_never_exposes_salt_or_ciphertext():
    secret_store.initialize("pw", initial_secrets={"A": "1"}, unlock_mode="cached")
    meta = secret_store.read_meta()
    assert "ciphertext" not in meta
    assert "salt" not in meta.get("kdf", {})
    assert meta["unlock_mode"] == "cached"
    assert meta["unlocked"] is True
