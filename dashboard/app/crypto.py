"""Master-password key derivation and authenticated encryption for secrets
at rest (Part 0 of the productionization work). Argon2id - already a
project dependency, used for the dashboard admin login in security.py -
is reused here as the KDF; AES-256-GCM (from `cryptography`) provides
authenticated encryption. Nothing in this module logs, reprs, or persists
a password or derived key; callers are responsible for scoping their
lifetime (see secret_store.py).

Wrong password is detected via the AEAD authentication tag failing to
verify (DecryptionError), never via a stored hash of the password itself -
there is nothing on disk that verifies a password guess offline any faster
than actually attempting a decrypt.
"""
from __future__ import annotations

import os

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_LEN = 32   # AES-256
SALT_LEN = 16
NONCE_LEN = 12
GCM_TAG_LEN = 16

# argon2id parameters for the *master password* KDF. Deliberately heavier
# than security.py's PasswordHasher() defaults (used for the dashboard
# admin login, checked on every request) since this only runs once per
# unlock. Benchmarked to land around 0.5-1.5s on the project's documented
# minimum spec (4 vCPU / 8GB, no GPU) - see docs/encryption.md.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 262144  # 256 MiB
ARGON2_PARALLELISM = 2


class DecryptionError(Exception):
    """Ciphertext failed to authenticate: wrong master password, or the
    stored blob was corrupted/tampered with. Never carries the attempted
    key or any plaintext in its message."""


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def derive_key(
    password: str,
    salt: bytes,
    *,
    time_cost: int | None = None,
    memory_cost_kib: int | None = None,
    parallelism: int | None = None,
) -> bytes:
    """Derives a 32-byte key from the master password. The caller owns the
    password string's lifetime; this function doesn't retain it.

    Unset parameters read the module-level ARGON2_* constants *at call
    time* (not as bound defaults), deliberately, so tests can trade
    strength for speed via `monkeypatch.setattr(crypto, "ARGON2_...", x)`
    without touching production's real parameters."""
    if not password:
        raise ValueError("password must not be empty")
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=time_cost if time_cost is not None else ARGON2_TIME_COST,
        memory_cost=memory_cost_kib if memory_cost_kib is not None else ARGON2_MEMORY_COST_KIB,
        parallelism=parallelism if parallelism is not None else ARGON2_PARALLELISM,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Returns nonce || ciphertext||tag (AES-256-GCM). A fresh random nonce
    is generated per call - never reuse a (key, nonce) pair."""
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def decrypt(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if len(key) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes")
    if len(blob) < NONCE_LEN + GCM_TAG_LEN:
        raise DecryptionError("ciphertext too short to be valid")
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise DecryptionError("wrong master password, or the secret store is corrupted") from exc


def zero(buf: bytearray) -> None:
    """Best-effort in-place zeroing of a mutable buffer. Python can't
    guarantee no other copy exists (immutable str/bytes objects, the GC,
    swap) - this reduces the window a key/password is readable in process
    memory, it is not a guarantee. See docs/encryption.md."""
    for i in range(len(buf)):
        buf[i] = 0
