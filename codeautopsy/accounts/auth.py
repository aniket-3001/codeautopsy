"""FastAPI auth dependencies for the two principals: Human (JWT) and Machine (API key).

Factories close over the app's account store + JWT secret so `create_app()` can wire them up
without a global singleton (keeps tests hermetic, one store per app instance).
"""

from __future__ import annotations

from collections.abc import Callable

import jwt
from fastapi import Header, HTTPException

from codeautopsy.accounts.models import User
from codeautopsy.accounts.security import decode_session_token
from codeautopsy.accounts.store import AccountStoreProtocol
from codeautopsy.config import Settings


class AuthContext:
    """What an authenticated request carries: who, and which org they may act as."""

    def __init__(self, user: User, org_id: str):
        self.user = user
        self.org_id = org_id


def make_require_user(
    store: AccountStoreProtocol, settings: Settings
) -> Callable[..., AuthContext]:
    def require_user(authorization: str = Header(default="")) -> AuthContext:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization[len("Bearer ") :]
        try:
            payload = decode_session_token(token, settings.jwt_secret)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="invalid or expired token") from exc
        user = store.get_user_by_id(payload["sub"])
        if user is None:
            raise HTTPException(status_code=401, detail="user no longer exists")
        return AuthContext(user=user, org_id=payload["org_id"])

    return require_user


def make_require_api_key(store: AccountStoreProtocol) -> Callable[..., str]:
    """Returns the org_id for a valid `X-Api-Key: ca_live_...` header."""

    def require_api_key(x_api_key: str = Header(default="")) -> str:
        org_id = store.resolve_api_key(x_api_key) if x_api_key else None
        if org_id is None:
            raise HTTPException(status_code=401, detail="missing or invalid API key")
        return org_id

    return require_api_key
