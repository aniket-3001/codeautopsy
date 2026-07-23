"""SQLite-backed accounts store: users, orgs, memberships, API keys.

Mirrors the shape of `provenance/store.py` (SQLite default, Postgres in prod via the same
`AccountStoreProtocol`). One user gets exactly one personal org at signup (D2 in
ARCHITECTURE.md); the membership table exists so teams can be added later without a migration.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from codeautopsy.accounts.models import ApiKeyPublic, CreateApiKeyResponse, Org, User
from codeautopsy.accounts.security import generate_api_key, hash_secret, verify_secret


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EmailAlreadyRegistered(Exception):
    pass


class AccountStoreProtocol(Protocol):
    """The tiny interface both the SQLite and Postgres backends satisfy."""

    def create_user_with_org(self, email: str, password: str) -> tuple[User, Org]: ...
    def verify_login(self, email: str, password: str) -> User | None: ...
    def get_user_by_id(self, user_id: str) -> User | None: ...
    def get_org_for_user(self, user_id: str) -> Org | None: ...
    def get_org_by_id(self, org_id: str) -> Org | None: ...
    def create_api_key(self, org_id: str) -> CreateApiKeyResponse: ...
    def list_api_keys(self, org_id: str) -> list[ApiKeyPublic]: ...
    def revoke_api_key(self, org_id: str, key_id: str) -> bool: ...
    def resolve_api_key(self, full_key: str) -> str | None: ...


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


class AccountStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # --- signup / login -------------------------------------------------------------
    def create_user_with_org(self, email: str, password: str) -> tuple[User, Org]:
        user_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        now = _now()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                raise EmailAlreadyRegistered(email)
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, email, hash_secret(password), now),
            )
            conn.execute(
                "INSERT INTO orgs (id, name, owner_user_id, created_at) VALUES (?, ?, ?, ?)",
                (org_id, f"{email}'s org", user_id, now),
            )
            conn.execute(
                "INSERT INTO memberships (org_id, user_id, role) VALUES (?, ?, 'owner')",
                (org_id, user_id),
            )
        return (
            User(id=user_id, email=email, created_at=now),
            Org(id=org_id, name=f"{email}'s org", owner_user_id=user_id, created_at=now),
        )

    def verify_login(self, email: str, password: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None or not verify_secret(password, row["password_hash"]):
            return None
        return User(id=row["id"], email=row["email"], created_at=row["created_at"])

    def get_user_by_id(self, user_id: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, email, created_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return User(**dict(row)) if row else None

    # --- orgs -------------------------------------------------------------------------
    def get_org_for_user(self, user_id: str) -> Org | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT o.id, o.name, o.owner_user_id, o.created_at FROM orgs o
                JOIN memberships m ON m.org_id = o.id
                WHERE m.user_id = ?
                ORDER BY o.created_at LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return Org(**dict(row)) if row else None

    def get_org_by_id(self, org_id: str) -> Org | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name, owner_user_id, created_at FROM orgs WHERE id = ?", (org_id,)
            ).fetchone()
        return Org(**dict(row)) if row else None

    # --- API keys -----------------------------------------------------------------
    def create_api_key(self, org_id: str) -> CreateApiKeyResponse:
        full_key, prefix, key_hash = generate_api_key()
        key_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, org_id, prefix, key_hash, _now()),
            )
        return CreateApiKeyResponse(id=key_id, key=full_key, prefix=prefix)

    def list_api_keys(self, org_id: str) -> list[ApiKeyPublic]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, key_prefix AS prefix, created_at, last_used_at FROM api_keys "
                "WHERE org_id = ? ORDER BY created_at",
                (org_id,),
            ).fetchall()
        return [ApiKeyPublic(**dict(r)) for r in rows]

    def revoke_api_key(self, org_id: str, key_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM api_keys WHERE id = ? AND org_id = ?", (key_id, org_id)
            )
        return cur.rowcount > 0

    def resolve_api_key(self, full_key: str) -> str | None:
        """Verify a presented `ca_live_<secret>` key and return its org_id, or None."""
        from codeautopsy.accounts.security import split_api_key

        secret = split_api_key(full_key)
        if secret is None:
            return None
        prefix = secret[:10]
        with self._conn() as conn:
            candidates = conn.execute(
                "SELECT id, org_id, key_hash FROM api_keys WHERE key_prefix = ?", (prefix,)
            ).fetchall()
            for row in candidates:
                if verify_secret(secret, row["key_hash"]):
                    conn.execute(
                        "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), row["id"])
                    )
                    return row["org_id"]
        return None
