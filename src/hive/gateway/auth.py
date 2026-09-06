"""
auth.py — shared-secret bearer auth for the gateway (PATTERN).

Single source of the token check so every protected route (and the WS handshake)
enforces it identically. Compared with hmac.compare_digest to avoid timing leaks.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException


def token_ok(token: str | None, secret: str) -> bool:
    # An empty configured secret must never authenticate an empty header.
    return bool(secret and secret.strip()) and token is not None and hmac.compare_digest(token, secret)


def make_auth_dependency(secret: str):
    """FastAPI dependency that 401s unless the X-Hive-Token header matches."""

    async def require_token(x_hive_token: str | None = Header(default=None)) -> None:
        if not token_ok(x_hive_token, secret):
            raise HTTPException(status_code=401, detail="bad token")

    return require_token


def make_approver_dependency(approver_key: str):
    """FastAPI dependency that accepts only the out-of-band approver key."""

    async def require_approver(x_hive_token: str | None = Header(default=None)) -> None:
        # An empty configured key must never authenticate an empty/missing header.
        if not approver_key or not token_ok(x_hive_token, approver_key):
            raise HTTPException(status_code=401, detail="bad approver token")

    return require_approver
