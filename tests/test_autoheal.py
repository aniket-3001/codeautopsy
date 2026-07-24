"""Unit tests for the Auto-Heal core: run lifecycle, dispatch, storage, org-scoping."""

from __future__ import annotations

import tempfile

import pytest

from codeautopsy.autoheal.core import (
    SEEDED_BUG_FILE,
    SEEDED_BUG_LINE,
    complete_heal_run,
    create_heal_run,
    dispatch_to_github,
    list_heal_runs,
)
from codeautopsy.autoheal.models import HealCompleteRequest, HealRun
from codeautopsy.config import Settings
from codeautopsy.provenance.store import ProvenanceStore


@pytest.fixture
def store() -> ProvenanceStore:
    return ProvenanceStore(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)


def _no_github() -> Settings:
    """Settings with no dispatch token — dispatch is a graceful no-op."""
    return Settings(HEAL_WEBHOOK_SECRET="test-secret")


def test_create_defaults_to_the_seeded_bug(store: ProvenanceStore) -> None:
    run = create_heal_run(store, org_id="o1", trigger="manual", settings=_no_github())
    assert run.file_path == SEEDED_BUG_FILE
    assert run.line == SEEDED_BUG_LINE
    assert run.trigger == "manual"
    # First timeline entry always records the triggering signal.
    assert run.events[0].label == "Crash signal received"


def test_dispatch_without_token_is_a_truthful_noop(store: ProvenanceStore) -> None:
    run = create_heal_run(store, org_id="o1", settings=_no_github())
    assert run.status == "dispatch_failed"
    assert any("dispatch skipped" in e.label.lower() for e in run.events)


def test_create_persists_the_run(store: ProvenanceStore) -> None:
    run = create_heal_run(store, org_id="o1", settings=_no_github())
    fetched = store.get_heal_run(run.run_id, org_id="o1")
    assert fetched is not None
    assert fetched.run_id == run.run_id
    assert fetched.status == run.status


def test_explicit_coordinates_are_kept(store: ProvenanceStore) -> None:
    run = create_heal_run(
        store,
        org_id="o1",
        commit_sha="abc123",
        file_path="pkg/thing.py",
        line=42,
        trigger="signoz-alert",
        settings=_no_github(),
    )
    assert (run.commit_sha, run.file_path, run.line) == ("abc123", "pkg/thing.py", 42)
    assert run.trigger == "signoz-alert"


def test_complete_records_success(store: ProvenanceStore) -> None:
    run = create_heal_run(store, org_id="o1", settings=_no_github())
    updated = complete_heal_run(
        store,
        run.run_id,
        HealCompleteRequest(
            status="succeeded",
            pr_url="https://github.com/x/y/pull/7",
            branch="autoheal/x",
            explanation="validated the discount code",
            lesson="never trust int()",
        ),
        org_id="o1",
    )
    assert updated is not None
    assert updated.status == "succeeded"
    assert updated.pr_url == "https://github.com/x/y/pull/7"
    assert updated.branch == "autoheal/x"
    assert any("PR opened" in e.label for e in updated.events)


def test_complete_records_failure(store: ProvenanceStore) -> None:
    run = create_heal_run(store, org_id="o1", settings=_no_github())
    updated = complete_heal_run(
        store,
        run.run_id,
        HealCompleteRequest(status="failed", detail="regression test still red"),
        org_id="o1",
    )
    assert updated is not None
    assert updated.status == "failed"
    assert any("could not verify" in e.label.lower() for e in updated.events)


def test_complete_unknown_run_returns_none(store: ProvenanceStore) -> None:
    assert (
        complete_heal_run(
            store, "heal_missing", HealCompleteRequest(status="succeeded"), org_id="o1"
        )
        is None
    )


def test_complete_is_org_scoped(store: ProvenanceStore) -> None:
    run = create_heal_run(store, org_id="o1", settings=_no_github())
    # A different org cannot complete o1's run.
    assert (
        complete_heal_run(
            store, run.run_id, HealCompleteRequest(status="succeeded"), org_id="o2"
        )
        is None
    )


def test_list_is_org_scoped_and_newest_first(store: ProvenanceStore) -> None:
    first = HealRun(org_id="o1", commit_sha="c1", file_path="a.py", line=1, created_at="2020-01-01T00:00:00+00:00")
    second = HealRun(org_id="o1", commit_sha="c2", file_path="b.py", line=2, created_at="2021-01-01T00:00:00+00:00")
    store.save_heal_run(first)
    store.save_heal_run(second)
    store.save_heal_run(HealRun(org_id="o2", commit_sha="c3", file_path="c.py", line=3))

    runs = list_heal_runs(store, org_id="o1").runs
    assert [r.run_id for r in runs] == [second.run_id, first.run_id]
    assert len(list_heal_runs(store, org_id="o2").runs) == 1


def test_dispatch_with_token_hits_github(store: ProvenanceStore, monkeypatch) -> None:
    calls: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    def _fake_post(url, json, headers, timeout):  # noqa: A002
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        return _Resp()

    monkeypatch.setattr("codeautopsy.autoheal.core.httpx.post", _fake_post)
    settings = Settings(
        HEAL_WEBHOOK_SECRET="s",
        CODEAUTOPSY_GITHUB_REPO="owner/repo",
        GITHUB_DISPATCH_TOKEN="ghp_x",
        CODEAUTOPSY_PUBLIC_BASE_URL="https://api.example.com",
    )
    run = HealRun(org_id="o1", commit_sha="c1", file_path="a.py", line=1)
    store.save_heal_run(run)
    updated = dispatch_to_github(store, run, settings)

    assert updated.status == "dispatched"
    assert calls["url"] == "https://api.github.com/repos/owner/repo/dispatches"
    assert calls["json"]["event_type"] == "autoheal"
    assert calls["json"]["client_payload"]["run_id"] == run.run_id
    assert (
        calls["json"]["client_payload"]["callback_url"]
        == f"https://api.example.com/v1/heal/{run.run_id}/complete"
    )
    assert calls["headers"]["Authorization"] == "Bearer ghp_x"
