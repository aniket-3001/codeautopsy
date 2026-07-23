"""Password hashing, API key generation, and JWT session tokens.

Two principals, two secrets, both hashed with argon2 (never stored in plaintext):
  - Human passwords (dashboard login).
  - Machine API key secrets (Recorder/Enricher/CI ingestion).
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()

API_KEY_PREFIX = "ca_live_"


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(secret: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, secret)
    except VerifyMismatchError:
        return False


def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key_to_show_once, lookup_prefix, secret_hash_to_store)."""
    secret = secrets.token_urlsafe(32)
    lookup_prefix = secret[:10]
    full_key = f"{API_KEY_PREFIX}{secret}"
    return full_key, lookup_prefix, hash_secret(secret)


def split_api_key(full_key: str) -> str | None:
    """Extract the raw secret from a presented `ca_live_<secret>` key, or None if malformed."""
    if not full_key.startswith(API_KEY_PREFIX):
        return None
    secret = full_key[len(API_KEY_PREFIX) :]
    return secret or None


def create_session_token(user_id: str, org_id: str, secret: str, expires_seconds: int) -> str:
    payload = {"sub": user_id, "org_id": org_id, "exp": int(time.time()) + expires_seconds}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_session_token(token: str, secret: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError (expired/invalid/tampered) — callers turn that into a 401."""
    return jwt.decode(token, secret, algorithms=["HS256"])
