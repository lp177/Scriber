"""Bearer-token auth for the public read/write REST API (``/api/v1``).

These tokens are distinct from the dashboard's HMAC session tokens: they are
long, opaque, alphanumeric secrets created from the dashboard, stored only as a
SHA-256 hash, and presented as ``Authorization: Bearer <token>``. A token has a
scope — ``read`` (GET only) or ``readwrite`` (GET plus writes).
"""

from __future__ import annotations

import hashlib
import secrets
import string

from fastapi import HTTPException, Request

from scriber import database

# Alphanumeric only ([A-Za-z0-9]) so a token survives being pasted into a chat
# without any character being escaped or mangled.
_ALPHABET = string.ascii_letters + string.digits
# 48 chars over a 62-symbol alphabet ≈ 285 bits of entropy — far beyond brute force.
_TOKEN_LENGTH = 48
# Non-secret leading chars shown in the dashboard so a token is recognizable.
PREFIX_LENGTH = 8

VALID_SCOPES = ("read", "readwrite")

_UNAUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def generate_token() -> str:
    """Return a fresh random alphanumeric API token."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LENGTH))


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a token (what we store and look up by)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """Return the non-secret leading chars used to label a token in the UI."""
    return token[:PREFIX_LENGTH]


def authenticate_bearer(authorization: str | None, client: str | None = None) -> dict | None:
    """Validate an ``Authorization: Bearer <token>`` header value.

    Transport-neutral core shared by the REST API dependencies below and the
    MCP server's auth middleware. On success, records the use (timestamp and
    client IP) and returns the token row; returns None when the header is
    missing/malformed or the token is unknown.
    """
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    row = database.get_api_token_by_hash(hash_token(token))
    if row is None:
        # constant-time-ish: we already hashed, so lookup time doesn't leak length
        return None
    database.touch_api_token(row["id"], client)
    return row


def _authenticate(request: Request) -> dict:
    """Validate the Bearer token, record its use, and return its row.

    Raises 401 when the header is missing or the token is unknown.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401, detail="Not authenticated", headers=_UNAUTH_HEADERS
        )
    row = authenticate_bearer(header, request.client.host if request.client else None)
    if row is None:
        raise HTTPException(
            status_code=401, detail="Invalid API token", headers=_UNAUTH_HEADERS
        )
    return row


async def require_api_token(request: Request) -> dict:
    """FastAPI dependency: any valid API token (read or read-write)."""
    return _authenticate(request)


async def require_api_token_rw(request: Request) -> dict:
    """FastAPI dependency: a valid API token with the ``readwrite`` scope."""
    row = _authenticate(request)
    if row.get("scope") != "readwrite":
        raise HTTPException(
            status_code=403, detail="This API token is read-only (needs read & write scope)."
        )
    return row


def normalize_scope(scope: str | None) -> str:
    """Validate and normalize a requested scope, defaulting to ``read``."""
    value = (scope or "read").strip().lower()
    if value not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(VALID_SCOPES)}")
    return value
