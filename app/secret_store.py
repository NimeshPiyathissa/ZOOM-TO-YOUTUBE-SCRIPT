"""Encrypted-at-rest secret store, gated by the operator's Master
Encryption Password (Part 0). Replaces plaintext storage of the YouTube
stream key, Zoom passcode/pwd and tk tokens, saved meeting data, VNC
password, webhook tokens, and any other secret with a single AES-256-GCM
blob (see crypto.py for the KDF/cipher). Non-secret config keeps living
where it already does (.env's non-secret keys, current-source.env,
settings.json's non-secret fields) - this module only ever holds values
that were secret before.

Two unlock modes (see docs/encryption.md for the full trade-off writeup
the operator chooses between at setup):

  "prompt"  - nothing usable sits on disk; the vault stays locked across a
              process restart until an admin submits the master password
              again (the dashboard's /unlock screen).
  "cached"  - the derived key is cached at config.MASTER_KEY_CACHE_FILE,
              root:dashboard 0640 (root can write it, only the dashboard
              service account can read it - the same group-membership
              privilege pattern this codebase already uses for zoombot
              access, not a new mechanism). Survives reboots. Protects
              against a stolen disk image, a leaked backup, or a
              non-root compromise - NOT against an attacker who already
              has root on this box, since root can always read the file
              group-readable by the service account it's about to run.

A manual `lock()` always works regardless of mode: it drops the in-memory
key/secrets in this process and writes a dashboard-owned sentinel file
that blocks cached-mode auto-unlock until an admin proves they still hold
the master password by unlocking again. Locking can't itself delete the
root-owned cache file (this process doesn't have permission to), so a
stolen cache file plus a stolen disk still only gets an attacker back to
where cached mode already was - the sentinel's job is to stop an
unattended *process restart* from silently re-opening a vault an admin
just told it to close.

Writing the cache file (setup, change_master_password, set_unlock_mode)
requires root and is only ever done from the root-invoked CLI
(`sudo python -m app.cli ...`) - the running dashboard web process never
attempts it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time

from . import config, crypto

logger = logging.getLogger("zoom-stream.secret_store")

STORE_VERSION = 1

_key: bytearray | None = None
_secrets: dict[str, str] | None = None


class VaultLockedError(Exception):
    """The secret store hasn't been unlocked in this process."""


class VaultNotInitializedError(Exception):
    """setup/initialize() has never been run - there is no store file yet."""


class VaultAlreadyInitializedError(Exception):
    """initialize() was called but a store already exists."""


# --------------------------------------------------------------- helpers

def _aad(version: int) -> bytes:
    return f"zoom-stream-secret-store:v{version}".encode("ascii")


def _load_raw() -> dict | None:
    if not config.SECRET_STORE_FILE.is_file():
        return None
    with open(config.SECRET_STORE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _chown_to_dashboard_user(path) -> None:
    """Best-effort: when this process is root (initialize()/change_master_
    password()/set_unlock_mode() are documented as root-invoked, for the
    cache file's sake - see module docstring), the secret store file must
    still end up owned by the `dashboard` service account, not root,
    because the long-running dashboard *web process* (which runs as
    `dashboard`, never as root) is what reads/writes it on every ordinary
    unlock/set_secrets call afterward. A root-owned, 0600 store file would
    make the vault unreadable by the very process meant to use it day to
    day. Silently a no-op on a non-root run (the file is already owned by
    whichever unprivileged user wrote it) and on Windows (no os.chown)."""
    if not hasattr(os, "chown") or not hasattr(os, "geteuid"):
        return
    if os.geteuid() != 0:
        return
    try:
        import pwd
        import grp
        uid = pwd.getpwnam(config.DASHBOARD_USER).pw_uid
        gid = grp.getgrnam(config.DASHBOARD_GROUP).gr_gid
        os.chown(path, uid, gid)
    except (KeyError, PermissionError, OSError) as exc:
        logger.warning(
            "could not chown %s to %s:%s - the dashboard service may not be able "
            "to read its own secret store until this is fixed manually (%s)",
            path, config.DASHBOARD_USER, config.DASHBOARD_GROUP, exc,
        )


def _write_raw(data: dict) -> None:
    path = config.SECRET_STORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".secrets_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        _chown_to_dashboard_user(tmp_path)
        os.replace(tmp_path, str(path))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _kdf_params_from_meta(raw: dict) -> dict:
    kdf = raw.get("kdf", {})
    return {
        "time_cost": int(kdf.get("time_cost", crypto.ARGON2_TIME_COST)),
        "memory_cost_kib": int(kdf.get("memory_cost_kib", crypto.ARGON2_MEMORY_COST_KIB)),
        "parallelism": int(kdf.get("parallelism", crypto.ARGON2_PARALLELISM)),
    }


def _new_kdf_block(salt: bytes) -> dict:
    return {
        "algo": "argon2id",
        "time_cost": crypto.ARGON2_TIME_COST,
        "memory_cost_kib": crypto.ARGON2_MEMORY_COST_KIB,
        "parallelism": crypto.ARGON2_PARALLELISM,
        "salt": base64.b64encode(salt).decode("ascii"),
    }


def _write_key_cache_file(key: bytes) -> None:
    path = config.MASTER_KEY_CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".master_key_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64encode(key))
        os.chmod(tmp_path, 0o640)
        if hasattr(os, "chown"):
            try:
                import grp
                gid = grp.getgrnam(config.DASHBOARD_GROUP).gr_gid
                os.chown(tmp_path, 0, gid)
            except (KeyError, PermissionError, OSError) as exc:
                os.unlink(tmp_path)
                raise PermissionError(
                    f"could not set root:{config.DASHBOARD_GROUP} ownership on the cached "
                    "master key - run this command as root (sudo)"
                ) from exc
        os.replace(tmp_path, str(path))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _read_key_cache_file() -> bytes | None:
    try:
        raw = config.MASTER_KEY_CACHE_FILE.read_bytes()
    except (FileNotFoundError, PermissionError):
        return None
    try:
        return base64.b64decode(raw)
    except Exception:
        logger.warning("cached master key file at %s is not valid base64 - ignoring", config.MASTER_KEY_CACHE_FILE)
        return None


def _clear_key_cache_file() -> None:
    try:
        config.MASTER_KEY_CACHE_FILE.unlink()
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        raise PermissionError("removing the cached master key requires root (sudo)") from exc


def _set_lock_sentinel() -> None:
    config.VAULT_LOCK_SENTINEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.VAULT_LOCK_SENTINEL_FILE.write_text(str(time.time()), encoding="utf-8")


def _clear_lock_sentinel() -> None:
    try:
        config.VAULT_LOCK_SENTINEL_FILE.unlink()
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------- state

def is_initialized() -> bool:
    return config.SECRET_STORE_FILE.is_file()


def is_unlocked() -> bool:
    return _secrets is not None


def read_meta() -> dict | None:
    """Non-secret metadata only (no salt, no ciphertext) - safe to expose
    over the API/UI even while locked."""
    raw = _load_raw()
    if raw is None:
        return None
    kdf_pub = dict(raw.get("kdf", {}))
    kdf_pub.pop("salt", None)
    return {
        "version": raw.get("version"),
        "cipher": raw.get("cipher"),
        "kdf": kdf_pub,
        "unlock_mode": raw.get("unlock_mode"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "unlocked": is_unlocked(),
        "manually_locked": config.VAULT_LOCK_SENTINEL_FILE.exists(),
    }


def _require_unlocked() -> dict[str, str]:
    if _secrets is None:
        raise VaultLockedError("the secret store is locked")
    return _secrets


# ------------------------------------------------------------- lifecycle

def initialize(password: str, *, initial_secrets: dict[str, str] | None = None,
                unlock_mode: str = config.DEFAULT_UNLOCK_MODE) -> None:
    """First-time setup. Refuses to run if a store already exists - use
    change_master_password() to rotate the password on an existing store,
    or the reset-vault CLI command to discard it and start over."""
    global _key, _secrets
    if unlock_mode not in config.UNLOCK_MODES:
        raise ValueError(f"unlock_mode must be one of {sorted(config.UNLOCK_MODES)}")
    if is_initialized():
        raise VaultAlreadyInitializedError(
            "a secret store already exists at " + str(config.SECRET_STORE_FILE)
        )
    salt = crypto.new_salt()
    key = crypto.derive_key(password, salt)
    secrets_dict = {str(k): str(v) for k, v in (initial_secrets or {}).items()}
    now = time.time()
    raw = {
        "version": STORE_VERSION,
        "cipher": "aes-256-gcm",
        "kdf": _new_kdf_block(salt),
        "unlock_mode": unlock_mode,
        "ciphertext": base64.b64encode(
            crypto.encrypt(key, json.dumps(secrets_dict).encode("utf-8"), _aad(STORE_VERSION))
        ).decode("ascii"),
        "created_at": now,
        "updated_at": now,
    }
    _write_raw(raw)
    if unlock_mode == "cached":
        _write_key_cache_file(key)
    _clear_lock_sentinel()
    _key = bytearray(key)
    _secrets = secrets_dict


def unlock(password: str) -> None:
    """Verifies the password via the AEAD tag (never a stored password
    hash) and, on success, loads the decrypted secrets into memory for
    this process. Raises crypto.DecryptionError on a wrong password."""
    global _key, _secrets
    raw = _load_raw()
    if raw is None:
        raise VaultNotInitializedError("no secret store found - run setup first")
    salt = base64.b64decode(raw["kdf"]["salt"])
    key = crypto.derive_key(password, salt, **_kdf_params_from_meta(raw))
    ciphertext = base64.b64decode(raw["ciphertext"])
    plaintext = crypto.decrypt(key, ciphertext, _aad(raw.get("version", STORE_VERSION)))
    _secrets = json.loads(plaintext.decode("utf-8"))
    _key = bytearray(key)
    _clear_lock_sentinel()


def lock() -> None:
    """Drops the in-memory key/secrets and blocks cached-mode auto-unlock
    until the next successful unlock() proves the master password again."""
    global _key, _secrets
    if _key is not None:
        crypto.zero(_key)
    _key = None
    _secrets = None
    _set_lock_sentinel()


def try_auto_unlock() -> bool:
    """Called once at process startup. Only succeeds in "cached" unlock
    mode, with the cache file present and readable, and no manual lock in
    effect. Never prompts, never raises on a routine "can't auto-unlock"
    outcome - callers should check is_unlocked() afterward and show the
    /unlock screen if it's still False."""
    global _key, _secrets
    if is_unlocked():
        return True
    raw = _load_raw()
    if raw is None or raw.get("unlock_mode") != "cached":
        return False
    if config.VAULT_LOCK_SENTINEL_FILE.exists():
        return False
    cached_key = _read_key_cache_file()
    if cached_key is None:
        return False
    try:
        ciphertext = base64.b64decode(raw["ciphertext"])
        plaintext = crypto.decrypt(cached_key, ciphertext, _aad(raw.get("version", STORE_VERSION)))
    except crypto.DecryptionError:
        logger.warning(
            "cached master key at %s failed to decrypt the secret store (stale cache "
            "after a change-master-password run elsewhere?) - staying locked",
            config.MASTER_KEY_CACHE_FILE,
        )
        return False
    _secrets = json.loads(plaintext.decode("utf-8"))
    _key = bytearray(cached_key)
    return True


# --------------------------------------------------------------- secrets

def get_secret(key: str, default: str = "") -> str:
    return _require_unlocked().get(key, default)


def get_all() -> dict[str, str]:
    return dict(_require_unlocked())


def set_secrets(updates: dict[str, str]) -> None:
    """Merges updates into the unlocked secrets and re-encrypts+persists
    immediately with the current key. Raises VaultLockedError if locked."""
    global _secrets
    current = _require_unlocked()
    if _key is None:
        raise VaultLockedError("the secret store is locked")
    merged = dict(current)
    merged.update({str(k): str(v) for k, v in updates.items()})
    raw = _load_raw()
    if raw is None:
        raise VaultNotInitializedError("no secret store found - run setup first")
    raw["ciphertext"] = base64.b64encode(
        crypto.encrypt(bytes(_key), json.dumps(merged).encode("utf-8"), _aad(raw.get("version", STORE_VERSION)))
    ).decode("ascii")
    raw["updated_at"] = time.time()
    _write_raw(raw)
    _secrets = merged


def change_master_password(old_password: str, new_password: str) -> None:
    """Verifies old_password by decrypting with it, re-encrypts the
    existing secrets under a freshly derived key with a new random salt,
    and (if unlock_mode is "cached") rewrites the key cache file. Meant
    to be run as root via the CLI, not from the web process."""
    global _key, _secrets
    raw = _load_raw()
    if raw is None:
        raise VaultNotInitializedError("no secret store found - run setup first")
    old_salt = base64.b64decode(raw["kdf"]["salt"])
    old_key = crypto.derive_key(old_password, old_salt, **_kdf_params_from_meta(raw))
    ciphertext = base64.b64decode(raw["ciphertext"])
    plaintext = crypto.decrypt(old_key, ciphertext, _aad(raw.get("version", STORE_VERSION)))
    secrets_dict = json.loads(plaintext.decode("utf-8"))

    new_salt = crypto.new_salt()
    new_key = crypto.derive_key(new_password, new_salt)
    raw["kdf"] = _new_kdf_block(new_salt)
    raw["ciphertext"] = base64.b64encode(
        crypto.encrypt(new_key, json.dumps(secrets_dict).encode("utf-8"), _aad(raw.get("version", STORE_VERSION)))
    ).decode("ascii")
    raw["updated_at"] = time.time()
    _write_raw(raw)

    if raw.get("unlock_mode") == "cached":
        _write_key_cache_file(new_key)

    _key = bytearray(new_key)
    _secrets = secrets_dict
    _clear_lock_sentinel()


def set_unlock_mode(new_mode: str, *, password: str) -> None:
    """Switches between "cached" and "prompt". Requires the current
    master password (proves authorization and lets us safely write/remove
    the cache file). Meant to be run as root via the CLI."""
    global _key, _secrets
    if new_mode not in config.UNLOCK_MODES:
        raise ValueError(f"unlock_mode must be one of {sorted(config.UNLOCK_MODES)}")
    raw = _load_raw()
    if raw is None:
        raise VaultNotInitializedError("no secret store found - run setup first")
    salt = base64.b64decode(raw["kdf"]["salt"])
    key = crypto.derive_key(password, salt, **_kdf_params_from_meta(raw))
    ciphertext = base64.b64decode(raw["ciphertext"])
    plaintext = crypto.decrypt(key, ciphertext, _aad(raw.get("version", STORE_VERSION)))

    raw["unlock_mode"] = new_mode
    raw["updated_at"] = time.time()
    _write_raw(raw)

    if new_mode == "cached":
        _write_key_cache_file(key)
    else:
        _clear_key_cache_file()

    _secrets = json.loads(plaintext.decode("utf-8"))
    _key = bytearray(key)
    _clear_lock_sentinel()


def reset_vault() -> None:
    """Irreversibly discards the encrypted store and its key cache. There
    is no cryptographic recovery for a lost master password - this is the
    only way forward, and it means every secret has to be re-entered
    afterward (run setup again). Only ever called from the reset-vault CLI
    command behind an explicit confirmation flag."""
    global _key, _secrets
    if _key is not None:
        crypto.zero(_key)
    _key = None
    _secrets = None
    try:
        config.SECRET_STORE_FILE.unlink()
    except FileNotFoundError:
        pass
    _clear_key_cache_file()
    _clear_lock_sentinel()
