"""Short-lived student authentication tokens and admin API-key checks.

Auth model (spec section 11): Exam Session ID + Student ID + short-lived
signed token. The token is minted server-side when a proctor enrolls a
student into a session (POST /api/sessions/{id}/students) and must be
presented by the Student Agent on WebSocket connect.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
from fastapi import Header, HTTPException, status

from config import settings


class TokenError(Exception):
    pass


def create_student_token(session_id: str, student_id: str) -> tuple[str, dt.datetime, str]:
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = now + dt.timedelta(minutes=settings.student_token_ttl_minutes)
    jti = str(uuid.uuid4())
    claims = {
        "sub": student_id,
        "session_id": session_id,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
        "scope": "student",
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at, jti


def verify_student_token(token: str, session_id: str, student_id: str) -> dict:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc

    if claims.get("scope") != "student":
        raise TokenError("wrong token scope")
    if claims.get("sub") != student_id:
        raise TokenError("token does not match student_id")
    if claims.get("session_id") != session_id:
        raise TokenError("token does not match session_id")
    return claims


def require_admin_api_key(x_admin_api_key: str = Header(default="")) -> None:
    """FastAPI dependency guarding admin REST endpoints."""
    if not x_admin_api_key or x_admin_api_key != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing admin API key")


def verify_admin_api_key(key: str) -> bool:
    """Used for the admin WebSocket handshake where headers aren't convenient (query param)."""
    return bool(key) and key == settings.admin_api_key
