"""Tests for the accounts/tenancy layer: users, orgs, memberships, API keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeautopsy.accounts.security import (
    generate_api_key,
    hash_secret,
    split_api_key,
    verify_secret,
)
from codeautopsy.accounts.store import AccountStore, EmailAlreadyRegistered


def _store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "accounts.db")


# --- security -------------------------------------------------------------------------------


def test_hash_and_verify_secret_roundtrip():
    hashed = hash_secret("correct horse battery staple")
    assert verify_secret("correct horse battery staple", hashed) is True
    assert verify_secret("wrong password", hashed) is False


def test_generate_and_split_api_key():
    full_key, prefix, key_hash = generate_api_key()
    assert full_key.startswith("ca_live_")
    secret = split_api_key(full_key)
    assert secret is not None
    assert secret.startswith(prefix)
    assert verify_secret(secret, key_hash) is True


def test_split_api_key_rejects_malformed_key():
    assert split_api_key("not-a-key") is None
    assert split_api_key("ca_live_") is None


# --- signup / login ---------------------------------------------------------------------


def test_create_user_with_org_and_login(tmp_path: Path):
    store = _store(tmp_path)
    user, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    assert org.owner_user_id == user.id

    logged_in = store.verify_login("dev@example.com", "hunter2pass")
    assert logged_in is not None
    assert logged_in.id == user.id


def test_login_rejects_wrong_password(tmp_path: Path):
    store = _store(tmp_path)
    store.create_user_with_org("dev@example.com", "hunter2pass")
    assert store.verify_login("dev@example.com", "wrongpass") is None


def test_login_rejects_unknown_email(tmp_path: Path):
    store = _store(tmp_path)
    assert store.verify_login("ghost@example.com", "whatever") is None


def test_signup_rejects_duplicate_email(tmp_path: Path):
    store = _store(tmp_path)
    store.create_user_with_org("dev@example.com", "hunter2pass")
    with pytest.raises(EmailAlreadyRegistered):
        store.create_user_with_org("dev@example.com", "anotherpass")


def test_get_user_by_id(tmp_path: Path):
    store = _store(tmp_path)
    user, _ = store.create_user_with_org("dev@example.com", "hunter2pass")
    fetched = store.get_user_by_id(user.id)
    assert fetched is not None
    assert fetched.email == "dev@example.com"
    assert store.get_user_by_id("does-not-exist") is None


def test_get_org_for_user_and_by_id(tmp_path: Path):
    store = _store(tmp_path)
    user, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    assert store.get_org_for_user(user.id).id == org.id
    assert store.get_org_by_id(org.id).id == org.id
    assert store.get_org_by_id("nope") is None


# --- API keys ---------------------------------------------------------------------------


def test_create_list_and_revoke_api_key(tmp_path: Path):
    store = _store(tmp_path)
    _, org = store.create_user_with_org("dev@example.com", "hunter2pass")

    created = store.create_api_key(org.id)
    assert created.key.startswith("ca_live_")

    keys = store.list_api_keys(org.id)
    assert len(keys) == 1
    assert keys[0].id == created.id
    assert keys[0].prefix == created.prefix

    assert store.revoke_api_key(org.id, created.id) is True
    assert store.list_api_keys(org.id) == []


def test_revoke_api_key_wrong_org_is_a_noop(tmp_path: Path):
    store = _store(tmp_path)
    _, org_a = store.create_user_with_org("a@example.com", "hunter2pass")
    _, org_b = store.create_user_with_org("b@example.com", "hunter2pass")
    created = store.create_api_key(org_a.id)

    assert store.revoke_api_key(org_b.id, created.id) is False
    assert len(store.list_api_keys(org_a.id)) == 1


def test_resolve_api_key_returns_owning_org(tmp_path: Path):
    store = _store(tmp_path)
    _, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    created = store.create_api_key(org.id)

    assert store.resolve_api_key(created.key) == org.id


def test_resolve_api_key_rejects_wrong_secret_and_malformed(tmp_path: Path):
    store = _store(tmp_path)
    _, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    store.create_api_key(org.id)

    assert store.resolve_api_key("ca_live_totallywrongsecret") is None
    assert store.resolve_api_key("not-even-a-key") is None


def test_resolve_api_key_after_revocation_fails(tmp_path: Path):
    store = _store(tmp_path)
    _, org = store.create_user_with_org("dev@example.com", "hunter2pass")
    created = store.create_api_key(org.id)
    store.revoke_api_key(org.id, created.id)

    assert store.resolve_api_key(created.key) is None
