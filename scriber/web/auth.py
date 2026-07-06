"""Stateless HMAC-signed token auth for the Scriber admin dashboard."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from scriber import config


def _sign(payload: str, secret: str) -> str:
    """Return the hex HMAC-SHA256 signature of *payload* keyed with *secret*."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_token(username: str, secret: str, ttl_seconds: int = 86400) -> str:
    """Create a signed token: b64url("username:expiry") + "." + hex HMAC-SHA256."""
    expiry = int(time.time()) + ttl_seconds
    payload = f"{username}:{expiry}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{encoded}.{_sign(payload, secret)}"


def verify_token(token: str, secret: str) -> str | None:
    """Return the username if *token* is validly signed and unexpired, else None."""
    encoded, sep, signature = token.partition(".")
    if not sep:
        return None
    try:
        payload = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not hmac.compare_digest(_sign(payload, secret), signature):
        return None
    username, sep, expiry_str = payload.rpartition(":")
    if not sep:
        return None
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if time.time() > expiry:
        return None
    return username


async def require_auth(request: Request) -> str:
    """FastAPI dependency: validate the Bearer token and return the username.

    Raises HTTPException(401) when the header is missing or the token is invalid.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = verify_token(token.strip(), config.get().web_secret)
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return username
