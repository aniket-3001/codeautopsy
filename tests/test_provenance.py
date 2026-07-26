"""Tests for the provenance store and the git-blame join engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import codeautopsy.provenance.indexer as indexer_module
from codeautopsy.provenance.indexer import (
    blame_introducing_commit,
    blame_origin,
    index_records,
    resolve,
)
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


def test_delete_by_decision_id(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45, decision_id="dec_1"))
    store.add(_record("abc123", 40, 45, decision_id="dec_2"))

    assert store.delete("dec_1") == 1
    assert store.count() == 1
    assert store.delete("dec_1") == 0  # already gone -> no-op
    assert store.find_by_line("abc123", "app/payment.py", 42).decision_id == "dec_2"


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


# --- blame_origin: malformed / missing-repo edge cases --------------------------------------


def test_blame_origin_returns_none_when_git_fails(tmp_path: Path):
    # Not a git repo at all -> the git subprocess exits non-zero -> GitError caught -> None.
    assert blame_origin(tmp_path, "app.py", 1) is None


def test_blame_origin_returns_none_on_empty_output(monkeypatch):
    monkeypatch.setattr(indexer_module, "_git", lambda *a, **k: "")
    assert blame_origin("repo", "f.py", 1) is None


def test_blame_origin_returns_none_on_malformed_header(monkeypatch):
    monkeypatch.setattr(indexer_module, "_git", lambda *a, **k: "onlyonetoken\n")
    assert blame_origin("repo", "f.py", 1) is None


def test_blame_origin_returns_none_on_invalid_sha_chars(monkeypatch):
    monkeypatch.setattr(indexer_module, "_git", lambda *a, **k: "not-hex-sha! 1 1\n")
    assert blame_origin("repo", "f.py", 1) is None


def test_blame_origin_returns_none_on_non_numeric_orig_line(monkeypatch):
    monkeypatch.setattr(indexer_module, "_git", lambda *a, **k: "abc1234 notanumber 1\n")
    assert blame_origin("repo", "f.py", 1) is None


def test_resolve_blame_finds_commit_but_no_indexed_decision(tmp_path: Path):
    """Blame succeeds but nothing was ever indexed for that commit — a partial resolution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.co"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    store = ProvenanceStore(tmp_path / "p.db")
    resp = resolve(store, ResolveRequest(commit_sha=head, file_path="app.py", line=1), repo=repo)
    assert resp.resolved is False
    assert resp.introducing_commit == head
    assert "no AI decision is indexed" in resp.detail


def test_index_records_bulk_loads_into_store(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    n = index_records(store, [_record("abc123", 1, 1), _record("abc123", 2, 2, decision_id="d2")])
    assert n == 2
    assert store.count() == 2


def test_record_contains_line():
    rec = _record("abc123", 40, 45)
    assert rec.contains_line(40) is True
    assert rec.contains_line(45) is True
    assert rec.contains_line(39) is False
    assert rec.contains_line(46) is False


def test_record_risk_source_defaults_to_heuristic():
    assert _record("abc123", 40, 45).risk_source == "heuristic"


def test_record_risk_source_accepts_ai_judge():
    assert _record("abc123", 40, 45, risk_source="ai_judge").risk_source == "ai_judge"


def test_record_risk_source_rejects_unknown_values():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _record("abc123", 40, 45, risk_source="vibes")


# --- tamper-evidence: the hash chain -----------------------------------------------------


def test_add_chains_records_by_org(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45, decision_id="d1"))
    store.add(_record("abc123", 50, 55, decision_id="d2"))
    rows = store.chain_rows()
    assert [r[0].decision_id for r in rows] == ["d1", "d2"]
    assert [r[3] for r in rows] == [1, 2]  # chain_seq
    # d2's prev_hash must equal d1's record_hash — the actual link.
    assert rows[1][1] == rows[0][2]


def test_chain_rows_scoped_per_org(tmp_path: Path):
    """Two orgs writing concurrently must not cross-link — each tenant gets its own chain."""
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45, decision_id="d1", org_id="org-a"))
    store.add(_record("abc123", 50, 55, decision_id="d2", org_id="org-b"))
    store.add(_record("abc123", 60, 65, decision_id="d3", org_id="org-a"))
    a_rows = store.chain_rows("org-a")
    b_rows = store.chain_rows("org-b")
    assert [r[0].decision_id for r in a_rows] == ["d1", "d3"]
    assert [r[0].decision_id for r in b_rows] == ["d2"]
    assert [r[3] for r in a_rows] == [1, 2]
    assert [r[3] for r in b_rows] == [1]


def test_verify_integrity_valid_on_untouched_store(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45, decision_id="d1"))
    store.add(_record("abc123", 50, 55, decision_id="d2"))
    result = store.verify_integrity()
    assert result.valid is True
    assert result.length == 2


def test_verify_integrity_detects_a_row_edited_outside_the_store_api(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("abc123", 40, 45, decision_id="d1"))
    with store._conn() as conn:  # noqa: SLF001 — deliberately bypassing add() to simulate tampering
        conn.execute(
            "UPDATE provenance SET reasoning_summary = ? WHERE decision_id = ?",
            ("this reasoning was never actually written", "d1"),
        )
    result = store.verify_integrity()
    assert result.valid is False
    assert result.broken_at == "d1"


def test_verify_integrity_empty_store_is_valid(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    result = store.verify_integrity()
    assert result.valid is True
    assert result.length == 0


def test_pre_chain_legacy_rows_are_excluded_not_backfilled(tmp_path: Path):
    """A row inserted before this feature existed (chain_seq defaults to 0) must not be
    silently folded into the chain — there's no real previous hash to anchor it to."""
    store = ProvenanceStore(tmp_path / "p.db")
    with store._conn() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO provenance (org_id, commit_sha, file_path, line_start, line_end, "
            "decision_span_id, decision_trace_id, session_id, reasoning_summary, risk_flags, "
            "model, tool, decision_id, created_at) VALUES "
            "('demo-public', 'abc123', 'app/payment.py', 1, 1, 'span', 'trace', 'sess', "
            "'legacy row', '[]', '', 'claude-code', 'dec_legacy', '2026-01-01T00:00:00+00:00')"
        )
    assert store.count() == 1
    assert store.chain_rows() == []
    result = store.verify_integrity()
    assert result.valid is True
    assert result.length == 0
