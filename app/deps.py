"""FastAPI dependencies: current session, auth enforcement, CSRF checks."""
from __future__ import annotations

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
