"""Pydantic models for the accounts/tenancy layer."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(UTC).isoformat()


class User(BaseModel):
    id: str
    email: str
    created_at: str = Field(default_factory=_now)


class Org(BaseModel):
    id: str
    name: str
    owner_user_id: str
    created_at: str = Field(default_factory=_now)


class ApiKeyPublic(BaseModel):
    """What we ever show back about a key — never the secret or its hash."""

    id: str
    prefix: str
    created_at: str
    last_used_at: str | None = None


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    org_id: str


class MeResponse(BaseModel):
    user: User
    org: Org


class CreateApiKeyResponse(BaseModel):
    id: str
    key: str  # the full secret — returned once, at creation time only
    prefix: str
