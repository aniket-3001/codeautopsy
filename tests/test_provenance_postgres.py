"""Tests for the Postgres-backed provenance store.

Requires a live Postgres via `DATABASE_URL` (provided by a service container in CI); skipped
locally when that's not set, mirroring `tests/test_provenance.py`'s SQLite coverage.
"""

from __future__ import annotations

import os

import pytest

from codeautopsy.provenance.models import ProvenanceRecord

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a live Postgres (DATABASE_URL)"
)


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


@pytest.fixture
def store():
    import psycopg

    from codeautopsy.provenance.store_postgres import PostgresProvenanceStore

    dsn = os.environ["DATABASE_URL"]
    s = PostgresProvenanceStore(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE TABLE provenance")
    return s


def test_store_roundtrip(store):
    assert store.count() == 0
    store.add(_record("abc123", 40, 45))
    assert store.count() == 1
    rec = store.find_by_line("abc123", "app/payment.py", 42)
    assert rec is not None
    assert rec.reasoning_summary == "assuming the input is always valid"
    assert rec.risk_flags == ["assumed_valid_input"]


def test_find_by_line_out_of_range(store):
    store.add(_record("abc123", 40, 45))
    assert store.find_by_line("abc123", "app/payment.py", 99) is None
    assert store.find_by_line("othersha", "app/payment.py", 42) is None


def test_last_writer_wins(store):
    store.add(_record("abc123", 40, 45, decision_id="old", created_at="2026-07-23T10:00:00+00:00"))
    store.add(_record("abc123", 41, 43, decision_id="new", created_at="2026-07-23T12:00:00+00:00"))
    rec = store.find_by_line("abc123", "app/payment.py", 42)
    assert rec is not None and rec.decision_id == "new"


def test_delete_by_decision_id(store):
    store.add(_record("abc123", 40, 45, decision_id="dec_1"))
    store.add(_record("abc123", 40, 45, decision_id="dec_2"))

    assert store.delete("dec_1") == 1
    assert store.count() == 1
    assert store.delete("dec_1") == 0
    assert store.find_by_line("abc123", "app/payment.py", 42).decision_id == "dec_2"


def test_add_many_and_all(store):
    n = store.add_many([_record("abc123", 1, 1), _record("abc123", 2, 2, decision_id="d2")])
    assert n == 2
    assert store.count() == 2
    all_records = store.all()
    assert len(all_records) == 2
    assert {r.decision_id for r in all_records} == {"dec_7f3a", "d2"}


def test_add_chains_records_and_verify_integrity(store):
    store.add(_record("abc123", 40, 45, decision_id="d1"))
    store.add(_record("abc123", 50, 55, decision_id="d2"))
    rows = store.chain_rows()
    assert [r[0].decision_id for r in rows] == ["d1", "d2"]
    assert [r[3] for r in rows] == [1, 2]
    assert rows[1][1] == rows[0][2]  # d2's prev_hash == d1's record_hash

    result = store.verify_integrity()
    assert result.valid is True
    assert result.length == 2


def test_verify_integrity_detects_a_row_edited_outside_the_store_api(store):
    import psycopg

    store.add(_record("abc123", 40, 45, decision_id="d1"))
    with psycopg.connect(store.dsn) as conn:
        conn.execute(
            "UPDATE provenance SET reasoning_summary = %s WHERE decision_id = %s",
            ("this reasoning was never actually written", "d1"),
        )
    result = store.verify_integrity()
    assert result.valid is False
    assert result.broken_at == "d1"
