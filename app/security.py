"""Password hashing, session lifecycle, CSRF tokens, login rate limiting.
No secrets (passwords, tokens) are ever logged."""
from __future__ import annotations

import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

from . import config, db

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    return True


# --- Sessions ---

def create_session(user_id: int, ip: str) -> tuple[str, str]:
    """Returns (session_id, csrf_token)."""
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = time.time()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, csrf_token, created_at, last_seen_at, expires_at, ip) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, user_id, csrf_token, now, now,
             now + config.SESSION_ABSOLUTE_TIMEOUT_SECONDS, ip),
        )
    return session_id, csrf_token


def load_session(session_id: str) -> dict | None:
    if not session_id:
        return None
    now = time.time()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now:
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return None
        if now - row["last_seen_at"] > config.SESSION_IDLE_TIMEOUT_SECONDS:
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return None
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now, session_id))
        return dict(row)


def destroy_session(session_id: str) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def destroy_all_sessions_for_user(user_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


# --- Login rate limiting ---

def record_login_attempt(ip: str, username: str, success: bool) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO login_attempts (ip, username, ts, success) VALUES (?,?,?,?)",
            (ip, username, time.time(), int(success)),
        )


def is_locked_out(ip: str, username: str) -> bool:
    cutoff = time.time() - config.LOGIN_LOCKOUT_WINDOW_SECONDS
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts "
            "WHERE ip=? AND username=? AND success=0 AND ts > ?",
            (ip, username, cutoff),
        ).fetchone()
        return row["n"] >= config.LOGIN_MAX_FAILURES


def clear_login_failures(ip: str, username: str) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ip=? AND username=?", (ip, username))
