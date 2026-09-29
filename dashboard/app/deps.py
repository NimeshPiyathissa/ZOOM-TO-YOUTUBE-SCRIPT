"""FastAPI dependencies: current session, auth enforcement, CSRF checks,
per-action rate limiting."""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from . import config, security


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def get_session(request: Request) -> dict | None:
    session_id = request.cookies.get(config.COOKIE_NAME)
    if not session_id:
        return None
    return security.load_session(session_id)


def require_session_api(request: Request) -> dict:
    """For JSON/API routes: 401 if not logged in."""
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return session


def require_csrf(request: Request, session: dict) -> None:
    header_token = request.headers.get("x-csrf-token", "")
    if not header_token or header_token != session.get("csrf_token"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad CSRF token")


# --- Action rate limiting (Part 3: touch controls) ---
#
# In-memory sliding window per (username, action) - deliberately not the
# DB-backed login_attempts table: this guards against a rapid double-tap
# or a stuck client retrying a control endpoint, not something that needs
# to survive a dashboard.service restart or be queryable later (the audit
# log is the permanent record of what actually happened).
_rate_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)


def require_rate_limit(session: dict, action: str, max_calls: int = 6, window_seconds: float = 10.0) -> None:
    key = (session.get("username", ""), action)
    now = time.time()
    bucket = _rate_buckets[key]
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_calls:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many '{action}' requests - wait a moment and try again",
        )
    bucket.append(now)
