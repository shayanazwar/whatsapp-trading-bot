from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

settings = get_settings()
serializer = URLSafeTimedSerializer(settings.app_secret_key, salt="kronos-mexc-admin-session-v1")


def issue_session(response: Response, username: str) -> str:
    csrf = secrets.token_urlsafe(32)
    token = serializer.dumps({"sub": username, "csrf": csrf})
    response.set_cookie(
        "kronos_session", token, max_age=settings.session_max_age_seconds,
        httponly=True, secure=settings.is_production, samesite="lax", path="/",
    )
    response.set_cookie(
        "kronos_csrf", csrf, max_age=settings.session_max_age_seconds,
        httponly=False, secure=settings.is_production, samesite="lax", path="/",
    )
    return csrf


def require_admin(
    request: Request,
    session_cookie: Annotated[str | None, Cookie(alias="kronos_session")] = None,
) -> str:
    if not session_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    try:
        data = serializer.loads(session_cookie, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired") from None
    if data.get("sub") != settings.admin_username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf_cookie = request.cookies.get("kronos_csrf")
        csrf_header = request.headers.get("X-CSRF-Token")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
        if not secrets.compare_digest(str(data.get("csrf", "")), csrf_header):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return str(data["sub"])


AdminUser = Annotated[str, Depends(require_admin)]
