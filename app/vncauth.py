"""VNC (RFB) authentication, performed server-side by the proxy.

The dashboard's /vnc/ws proxy is already gated by an authenticated
dashboard session and only ever reaches x11vnc over loopback, so making
the *browser* answer a second, separate VNC password challenge is pure
friction - and it leaks the display password into every viewer's hands.
Instead the proxy answers x11vnc's challenge itself, using the password
already stored server-side (VNC_PASSWORD in the zoombot .env, read via
env_store), and offers the browser the "None" security type. The secret
never leaves the box.

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
    """The configured VNC password, read fresh from the zoombot .env
    (so a rotation via the Configuration page takes effect without a
    dashboard restart). Empty string if unset."""
    return env_store.read_parsed().get("VNC_PASSWORD", "")
