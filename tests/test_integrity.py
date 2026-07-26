"""Tests for the provenance hash chain — pure functions, no store/DB needed."""

from __future__ import annotations

from codeautopsy.provenance.integrity import (
    GENESIS_HASH,
    ChainRow,
    canonicalize,
    compute_hash,
    verify_chain,
)
from codeautopsy.provenance.models import ProvenanceRecord


def _record(**over: object) -> ProvenanceRecord:
    base = dict(
        org_id="demo-public",
        commit_sha="abc123def456",
        file_path="app/payment.py",
        line_start=40,
        line_end=46,
        decision_span_id="00f067aa0ba902b7",
        decision_trace_id="7b5b1b39741a991f073d59e245fb7575",
        session_id="sess-1",
        reasoning_summary="cast the discount code straight to int",
        risk_flags=["unvalidated-input"],
        decision_id="dec_abc",
    )
    base.update(over)
    return ProvenanceRecord(**base)


def test_canonicalize_is_deterministic() -> None:
    rec = _record()
    assert canonicalize(rec) == canonicalize(_record())


def test_canonicalize_changes_with_content() -> None:
    a = canonicalize(_record())
    b = canonicalize(_record(reasoning_summary="a different reason entirely"))
    assert a != b


def test_compute_hash_changes_with_prev_hash() -> None:
    rec = _record()
    h1 = compute_hash(GENESIS_HASH, rec)
    h2 = compute_hash("some-other-prev-hash", rec)
    assert h1 != h2


def test_compute_hash_changes_with_content() -> None:
    h1 = compute_hash(GENESIS_HASH, _record())
    h2 = compute_hash(GENESIS_HASH, _record(reasoning_summary="different"))
    assert h1 != h2


def test_compute_hash_is_64_hex_chars() -> None:
    h = compute_hash(GENESIS_HASH, _record())
    assert len(h) == 64
    int(h, 16)  # raises ValueError if not valid hex


def test_verify_chain_empty_is_valid() -> None:
    result = verify_chain([])
    assert result.valid is True
    assert result.length == 0


def test_verify_chain_valid_two_link_chain() -> None:
    r1 = _record(decision_id="d1")
    h1 = compute_hash(GENESIS_HASH, r1)
    r2 = _record(decision_id="d2")
    h2 = compute_hash(h1, r2)
    rows: list[ChainRow] = [
        (r1, GENESIS_HASH, h1, 1),
        (r2, h1, h2, 2),
    ]
    result = verify_chain(rows)
    assert result.valid is True
    assert result.length == 2
    assert result.broken_at is None


def test_verify_chain_detects_altered_content() -> None:
    r1 = _record(decision_id="d1")
    h1 = compute_hash(GENESIS_HASH, r1)
    # The stored hash still reflects the original reasoning; the record itself was mutated
    # after the fact (simulating a row edited directly in the DB).
    tampered = r1.model_copy(update={"reasoning_summary": "this was never actually said"})
    rows: list[ChainRow] = [(tampered, GENESIS_HASH, h1, 1)]
    result = verify_chain(rows)
    assert result.valid is False
    assert result.broken_at == "d1"
    assert "altered" in result.detail


def test_verify_chain_detects_broken_prev_hash_link() -> None:
    r1 = _record(decision_id="d1")
    h1 = compute_hash(GENESIS_HASH, r1)
    r2 = _record(decision_id="d2")
    h2 = compute_hash(h1, r2)
    # r2's stored prev_hash doesn't match r1's actual hash — as if a record were deleted
    # or reordered between them.
    rows: list[ChainRow] = [
        (r1, GENESIS_HASH, h1, 1),
        (r2, "not-actually-h1", h2, 2),
    ]
    result = verify_chain(rows)
    assert result.valid is False
    assert result.broken_at == "d2"
