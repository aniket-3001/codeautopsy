"""Tests for the Postgres-backed accounts store.

Requires a live Postgres via `DATABASE_URL` (provided by a service container in CI); skipped
locally when that's not set, mirroring `tests/test_provenance_postgres.py`.
"""

from __future__ import annotations

import os

import pytest

from codeautopsy.accounts.store import EmailAlreadyRegistered

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a live Postgres (DATABASE_URL)"
)


@pytest.fixture
def store():
    import psycopg

    from codeautopsy.accounts.store_postgres import PostgresAccountStore

    dsn = os.environ["DATABASE_URL"]
    s = PostgresAccountStore(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE TABLE users, orgs, memberships, api_keys")
    return s


def test_create_user_with_org_and_login(store):
    user, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    assert org.owner_user_id == user.id
    assert store.verify_login("dev@example.com", "hunter2pass").id == user.id
    assert store.verify_login("dev@example.com", "wrongpass") is None


def test_signup_rejects_duplicate_email(store):
    store.create_user_with_org("dev@example.com", "hunter2pass")
    with pytest.raises(EmailAlreadyRegistered):
        store.create_user_with_org("dev@example.com", "anotherpass")


def test_get_user_and_org_lookups(store):
    user, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    assert store.get_user_by_id(user.id).email == "dev@example.com"
    assert store.get_user_by_id("nope") is None
    assert store.get_org_for_user(user.id).id == org.id
    assert store.get_org_by_id(org.id).id == org.id
    assert store.get_org_by_id("nope") is None


def test_api_key_lifecycle(store):
    _, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    created = store.create_api_key(org.id)

    assert store.resolve_api_key(created.key) == org.id
    assert len(store.list_api_keys(org.id)) == 1

    assert store.revoke_api_key(org.id, created.id) is True
    assert store.list_api_keys(org.id) == []
    assert store.resolve_api_key(created.key) is None


def test_resolve_api_key_rejects_wrong_secret(store):
    _, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    store.create_api_key(org.id)
    assert store.resolve_api_key("ca_live_totallywrongsecret") is None
    assert store.resolve_api_key("not-even-a-key") is None
