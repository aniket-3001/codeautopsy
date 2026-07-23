"""Tests for the provenance store and the git-blame join engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeautopsy.provenance.indexer import blame_introducing_commit, resolve
from codeautopsy.provenance.models import ProvenanceRecord, ResolveRequest
from codeautopsy.provenance.store import ProvenanceStore


def _record(commit: str, start: int, end: int, **kw) -> ProvenanceRecord:
    base = dict(
        commit_sha=commit,
        file_path="app/payment.py",
        line_start=start,
        line_end=end,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="sess_test",
        reasoning_summary="assuming the input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_7f3a",
    )
    base.update(kw)
    return ProvenanceRecord(**base)


def test_store_roundtrip(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    assert store.count() == 0
    store.add(_record("abc123", 40, 45))
    assert store.count() == 1
    rec = store.find_by_line("abc123", "app/payment.py", 42)
    assert rec is not None
    assert rec.reasoning_summary == "assuming the input is always valid"
    assert rec.risk_flags == ["assumed_valid_input"]


def test_find_by_line_out_of_range(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45))
    assert store.find_by_line("abc123", "app/payment.py", 99) is None
    assert store.find_by_line("othersha", "app/payment.py", 42) is None


def test_last_writer_wins(tmp_path: Path):
    """Overlapping decisions on the same line -> most recent one is returned."""
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45, decision_id="old", created_at="2026-07-23T10:00:00+00:00"))
    store.add(_record("abc123", 41, 43, decision_id="new", created_at="2026-07-23T12:00:00+00:00"))
    rec = store.find_by_line("abc123", "app/payment.py", 42)
    assert rec is not None and rec.decision_id == "new"


def test_resolve_fast_path(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("deployedsha", 40, 45))
    resp = resolve(store, ResolveRequest(commit_sha="deployedsha", file_path="app/payment.py", line=42))
    assert resp.resolved is True
    assert resp.introducing_commit == "deployedsha"
    assert resp.record is not None and resp.record.decision_id == "dec_7f3a"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_blame_join_end_to_end(tmp_path: Path):
    """The real thing: deploy commit != introducing commit; blame bridges the gap."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")

    payment = repo / "app" / "payment.py"

    # Commit 1: introduce the buggy line at line 1.
    payment.write_text("discount = int(code)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add discount parse")
    introducing = _git(repo, "rev-parse", "HEAD")

    # Commit 2 (the deploy): prepend an unrelated line, pushing the buggy line to line 2.
    payment.write_text("import logging\ndiscount = int(code)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add logging import")
    deployed = _git(repo, "rev-parse", "HEAD")

    assert introducing != deployed

    # Blame at the deployed commit for the (now shifted) buggy line must find commit 1.
    got = blame_introducing_commit(repo, "app/payment.py", 2, deployed)
    assert got == introducing

    # Record the decision against the INTRODUCING commit, then resolve from the DEPLOYED one.
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record(introducing, 1, 1))
    resp = resolve(
        store,
        ResolveRequest(commit_sha=deployed, file_path="app/payment.py", line=2),
        repo=repo,
    )
    assert resp.resolved is True
    assert resp.introducing_commit == introducing
    assert resp.record is not None
    assert resp.record.reasoning_summary == "assuming the input is always valid"
