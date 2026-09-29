"""VNC (RFB) authentication, performed server-side by the proxy.

The dashboard's /vnc/ws proxy is already gated by an authenticated
dashboard session and only ever reaches x11vnc over loopback, so making
the *browser* answer a second, separate VNC password challenge is pure
friction - and it leaks the display password into every viewer's hands.
Instead the proxy answers x11vnc's challenge itself, using the password
already stored server-side, and offers the browser the "None" security
type. The secret never leaves the box.

Part 0: VNC_PASSWORD's source of truth is now the encrypted vault. The
legacy settings.json/.env fallbacks below only matter for an install that
hasn't run through the vault-based setup/migration yet.

VNC's challenge-response is DES-ECB with one quirk: each byte of the
(8-char, null-padded) key has its bit order reversed before use. The
16-byte challenge is encrypted as two independent 8-byte ECB blocks.
"""
from __future__ import annotations

import pyDes

from . import env_store

# bit-reverse each byte: VNC's historical DES key quirk
_MIRROR = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


def _vnc_key(password: str) -> bytes:
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\x00")
    return bytes(_MIRROR[b] for b in raw)


def challenge_response(challenge: bytes, password: str) -> bytes:
    """16-byte DES response to x11vnc's 16-byte challenge."""
    if len(challenge) != 16:
        raise ValueError(f"VNC challenge must be 16 bytes, got {len(challenge)}")
    des = pyDes.des(_vnc_key(password), pyDes.ECB)
    return des.encrypt(challenge[:8]) + des.encrypt(challenge[8:16])


def current_password() -> str:
    """The configured VNC password. Vault first (the source of truth for
    any install that's completed Part 0 setup/migration); settings.json or
    the zoombot .env as a fallback for an install that hasn't yet. Empty
    string if unset anywhere."""
    try:
        from . import secret_store
        if secret_store.is_unlocked():
            pw = secret_store.get_secret("VNC_PASSWORD", "")
            if pw:
                return pw
    except Exception:
        pass
    try:
        from . import settings_store
        s = settings_store.load_settings()
        if s.get("vnc_password"):
            return str(s["vnc_password"])
    except Exception:
        pass
    try:
        return env_store.read_parsed().get("VNC_PASSWORD", "")
    except Exception:
        return ""
