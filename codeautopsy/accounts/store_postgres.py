"""Postgres-backed accounts store — persists across Cloud Run redeploys.

Same interface as `AccountStore` (SQLite); selected via `DATABASE_URL`, same as the
provenance store. See `provenance/store_postgres.py` for the sibling pattern.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg

from codeautopsy.accounts.models import ApiKeyPublic, CreateApiKeyResponse, Org, User
from codeautopsy.accounts.security import (
    generate_api_key,
    hash_secret,
    split_api_key,
    verify_secret,
)
from codeautopsy.accounts.store import EmailAlreadyRegistered


def _now() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orgs (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    org_id  TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role    TEXT NOT NULL DEFAULT 'owner',
    PRIMARY KEY (org_id, user_id)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,
    key_hash      TEXT NOT NULL,
    last_used_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_membership_user ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_api_key_prefix ON api_keys(key_prefix);
"""


class PostgresAccountStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._init_schema()

    def _init_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(_SCHEMA)

    def create_user_with_org(self, email: str, password: str) -> tuple[User, Org]:
        user_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        now = _now()
        with psycopg.connect(self.dsn) as conn:
            existing = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
            if existing:
                raise EmailAlreadyRegistered(email)
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (%s, %s, %s, %s)",
                (user_id, email, hash_secret(password), now),
            )
            conn.execute(
                "INSERT INTO orgs (id, name, owner_user_id, created_at) VALUES (%s, %s, %s, %s)",
                (org_id, f"{email}'s org", user_id, now),
            )
            conn.execute(
                "INSERT INTO memberships (org_id, user_id, role) VALUES (%s, %s, 'owner')",
                (org_id, user_id),
            )
        return (
            User(id=user_id, email=email, created_at=now),
            Org(id=org_id, name=f"{email}'s org", owner_user_id=user_id, created_at=now),
        )

    def verify_login(self, email: str, password: str) -> User | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE email = %s",
                (email,),
            ).fetchone()
        if row is None or not verify_secret(password, row[2]):
            return None
        return User(id=row[0], email=row[1], created_at=row[3])

    def get_user_by_id(self, user_id: str) -> User | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT id, email, created_at FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return User(id=row[0], email=row[1], created_at=row[2]) if row else None

    def get_org_for_user(self, user_id: str) -> Org | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """
                SELECT o.id, o.name, o.owner_user_id, o.created_at FROM orgs o
                JOIN memberships m ON m.org_id = o.id
                WHERE m.user_id = %s
                ORDER BY o.created_at LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return Org(id=row[0], name=row[1], owner_user_id=row[2], created_at=row[3]) if row else None

    def get_org_by_id(self, org_id: str) -> Org | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT id, name, owner_user_id, created_at FROM orgs WHERE id = %s", (org_id,)
            ).fetchone()
        return Org(id=row[0], name=row[1], owner_user_id=row[2], created_at=row[3]) if row else None

    def create_api_key(self, org_id: str) -> CreateApiKeyResponse:
        full_key, prefix, key_hash = generate_api_key()
        key_id = str(uuid.uuid4())
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (key_id, org_id, prefix, key_hash, _now()),
            )
        return CreateApiKeyResponse(id=key_id, key=full_key, prefix=prefix)

    def list_api_keys(self, org_id: str) -> list[ApiKeyPublic]:
        with psycopg.connect(self.dsn) as conn:
            rows = conn.execute(
                "SELECT id, key_prefix, created_at, last_used_at FROM api_keys "
                "WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            ).fetchall()
        return [
            ApiKeyPublic(id=r[0], prefix=r[1], created_at=r[2], last_used_at=r[3]) for r in rows
        ]

    def revoke_api_key(self, org_id: str, key_id: str) -> bool:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                "DELETE FROM api_keys WHERE id = %s AND org_id = %s", (key_id, org_id)
            )
        return cur.rowcount > 0

    def resolve_api_key(self, full_key: str) -> str | None:
        secret = split_api_key(full_key)
        if secret is None:
            return None
        prefix = secret[:10]
        with psycopg.connect(self.dsn) as conn:
            candidates = conn.execute(
                "SELECT id, org_id, key_hash FROM api_keys WHERE key_prefix = %s", (prefix,)
            ).fetchall()
            for row in candidates:
                if verify_secret(secret, row[2]):
                    conn.execute(
                        "UPDATE api_keys SET last_used_at = %s WHERE id = %s", (_now(), row[0])
                    )
                    return row[1]
        return None
