"""Materializes the pipeline's runtime env file - the merge of the
non-secret .env keys (still plain config on persistent disk, unchanged)
with the vault's current decrypted secrets - onto a tmpfs-backed path via
write-runtime-env.sh, so a decrypted secret is never written to persistent
disk, even transiently (Part 0's "runtime bridge" for the bash pipeline,
which reads secrets via `source` and can't call into the Python vault
directly).

Not wired into any call site yet - env_store.py/scripts/lib.sh still read
the plaintext .env directly until Phase A2's migration explicitly switches
production over. This module is deliberately usable standalone (see
tests/test_runtime_env.py) so that switch is a small, reviewable change
once it happens, not something invented under pressure on the live box.
"""
from __future__ import annotations

from . import config, env_store, secret_store
from .control import ControlError, SUDO, run_as_zoombot


def build_runtime_env() -> dict[str, str]:
    """Non-secret keys from the current .env, overlaid with the vault's
    current secret values for every key in config.SECRET_ENV_KEYS present
    in the vault. A vault key with no value yet (not migrated/not set)
    falls back to whatever .env already has for it, so a partially
    migrated install still produces a complete, working env file."""
    current = env_store.read_parsed()
    merged = {key: current.get(key, "") for key in config.ALL_ENV_KEYS}
    secrets = secret_store.get_all()
    for key in config.SECRET_ENV_KEYS:
        if key in secrets:
            merged[key] = secrets[key]
    return merged


def write_runtime_env() -> None:
    """Builds the merged env and writes it to the tmpfs path via the
    zoombot-owned write-runtime-env.sh (same sudo/stdin-piping contract as
    env_store.write_updates -> write-env.sh). Raises VaultLockedError if
    the vault isn't unlocked - there is nothing sensible to materialize
    while locked, and the caller should show the unlock screen instead of
    silently writing a runtime file missing its secrets."""
    merged = build_runtime_env()
    content = env_store._encode_env_pairs(merged)
    argv = [SUDO, "-u", config.ZOOMBOT_USER, str(config.WRITE_RUNTIME_ENV_SCRIPT)]
    proc = run_as_zoombot(argv, input_bytes=content, timeout=15)
    if proc.returncode != 0:
        raise ControlError("failed to write runtime env: " + proc.stderr.decode(errors="replace"))
