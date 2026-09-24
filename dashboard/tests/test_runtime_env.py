"""Tests for app/runtime_env.py (Part 0's tmpfs runtime bridge): merging
non-secret .env with the vault's current secrets, and writing that merge
out via the zoombot-owned write-runtime-env.sh script."""
from __future__ import annotations

import pytest

from app import config, env_store, runtime_env, secret_store


@pytest.fixture(autouse=True)
def _isolated_vault(tmp_path, monkeypatch):
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
    yield
    secret_store._key = None
    secret_store._secrets = None


def test_build_runtime_env_overlays_vault_secrets_on_nonsecret_env(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {
        "ZOOM_LINK": "https://zoom.us/j/123", "BOT_NAME": "Stream Bot",
        "RESOLUTION": "1920x1080", "YT_STREAM_KEY": "stale-plaintext-key",
    })
    secret_store.initialize("correct horse battery staple", initial_secrets={
        "YT_STREAM_KEY": "fresh-vault-key", "VNC_PASSWORD": "vncpw",
    }, unlock_mode="prompt")

    merged = runtime_env.build_runtime_env()

    assert merged["YT_STREAM_KEY"] == "fresh-vault-key"  # vault wins over stale .env
    assert merged["VNC_PASSWORD"] == "vncpw"  # vault-only key still shows up
    assert merged["BOT_NAME"] == "Stream Bot"  # non-secret passthrough
    assert merged["RESOLUTION"] == "1920x1080"
    assert set(merged.keys()) == set(config.ALL_ENV_KEYS)  # complete, source-compatible env


def test_build_runtime_env_falls_back_to_env_for_unmigrated_secret(monkeypatch):
    """A partially-migrated install (vault initialized but this particular
    key never moved into it) must still produce a complete, working env
    file - fall back to whatever .env already has."""
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"ZOOM_PASSCODE": "still-in-env"})
    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")

    merged = runtime_env.build_runtime_env()
    assert merged["ZOOM_PASSCODE"] == "still-in-env"


def test_build_runtime_env_requires_unlocked_vault(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {})
    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")
    secret_store.lock()
    with pytest.raises(secret_store.VaultLockedError):
        runtime_env.build_runtime_env()


def test_write_runtime_env_pipes_content_to_the_trusted_script(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"BOT_NAME": "Stream Bot"})
    secret_store.initialize("pw", initial_secrets={"YT_STREAM_KEY": "k"}, unlock_mode="prompt")

    calls = []

    class _FakeProc:
        returncode = 0
        stderr = b""

    def _fake_run(argv, input_bytes=None, timeout=15):
        calls.append((argv, input_bytes))
        return _FakeProc()

    monkeypatch.setattr(runtime_env, "run_as_zoombot", _fake_run)

    runtime_env.write_runtime_env()

    assert len(calls) == 1
    argv, input_bytes = calls[0]
    assert argv[-1] == str(config.WRITE_RUNTIME_ENV_SCRIPT)
    assert argv[1:3] == ["-u", config.ZOOMBOT_USER]
    assert b"YT_STREAM_KEY\x00k\x00" in input_bytes
    assert b"BOT_NAME\x00Stream Bot\x00" in input_bytes


def test_write_runtime_env_raises_on_script_failure(monkeypatch):
    monkeypatch.setattr(env_store, "read_parsed", lambda: {})
    secret_store.initialize("pw", initial_secrets={}, unlock_mode="prompt")

    class _FakeProc:
        returncode = 1
        stderr = b"permission denied"

    monkeypatch.setattr(runtime_env, "run_as_zoombot", lambda *a, **k: _FakeProc())

    with pytest.raises(runtime_env.ControlError):
        runtime_env.write_runtime_env()
