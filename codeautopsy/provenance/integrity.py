"""Tamper-evidence for the provenance chain.

Git commit trailers (`trailers.py`) already let provenance survive a dropped database by
round-tripping identity through git itself — but that only protects the *git* side. The
SQLite/Postgres rows the indexer, confidence scorer, and dashboards actually query are plain
mutable rows: nothing today detects if one gets edited after the fact.

Each record's hash commits to its own content plus the previous record's hash, chained
per-tenant in write order (`chain_seq`, assigned by the store). Recomputing the chain and
comparing it to what's stored either matches — proving nothing in the chain was altered since
ingestion — or diverges at a specific record, which is exactly the record that changed.

Deliberately stdlib-only (sha256 + json), same as the rest of the provenance layer's "keep it
boring" philosophy.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from codeautopsy.provenance.models import ProvenanceRecord

GENESIS_HASH = "0" * 64

# The content fields a hash commits to. Deliberately excludes org_id's own storage quirks
# (e.g. SQLite row id) and the hash columns themselves, so hashing is stable and reproducible
# from a `ProvenanceRecord` alone, independent of which backend stored it.
_HASHED_FIELDS = (
    "org_id",
    "commit_sha",
    "file_path",
    "line_start",
    "line_end",
    "decision_span_id",
    "decision_trace_id",
    "session_id",
    "reasoning_summary",
    "risk_flags",
    "risk_source",
    "model",
    "tool",
    "decision_id",
    "created_at",
)


def canonicalize(record: ProvenanceRecord) -> str:
    """Deterministic JSON of the fields a hash must commit to — same record, same bytes,
    regardless of dict/field ordering."""
    data = record.model_dump(include=set(_HASHED_FIELDS))
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def compute_hash(prev_hash: str, record: ProvenanceRecord) -> str:
    """sha256(prev_hash || canonical(record)) — the link in the chain this record forms."""
    payload = f"{prev_hash}:{canonicalize(record)}".encode()
    return hashlib.sha256(payload).hexdigest()


class ChainVerification(BaseModel):
    """The result of re-walking a tenant's hash chain and comparing it to what's stored."""

    org_id: str = ""
    length: int = 0
    valid: bool = True
    # decision_id of the first record where recomputation diverged from what's stored, if any.
    broken_at: str | None = None
    detail: str = ""


# One chain row as read back from a store: (record, prev_hash, record_hash, chain_seq),
# already ordered by chain_seq. Rows with chain_seq == 0 predate this feature and are excluded
# by the store before this function ever sees them.
ChainRow = tuple[ProvenanceRecord, str, str, int]


def verify_chain(rows: list[ChainRow], org_id: str = "demo-public") -> ChainVerification:
    """Recompute the hash chain over `rows` (already in chain order) and compare it against
    the `(prev_hash, record_hash)` each row claims. Pure — no store/DB access, so it's testable
    with hand-built rows."""
    if not rows:
        return ChainVerification(org_id=org_id, length=0, valid=True, detail="empty chain")

    expected_prev = GENESIS_HASH
    for record, prev_hash, record_hash, chain_seq in rows:
        if prev_hash != expected_prev:
            return ChainVerification(
                org_id=org_id,
                length=len(rows),
                valid=False,
                broken_at=record.decision_id,
                detail=(
                    f"chain_seq={chain_seq}: stored prev_hash does not match the previous "
                    "record's hash — a record may have been reordered or deleted"
                ),
            )
        recomputed = compute_hash(prev_hash, record)
        if recomputed != record_hash:
            return ChainVerification(
                org_id=org_id,
                length=len(rows),
                valid=False,
                broken_at=record.decision_id,
                detail=(
                    f"chain_seq={chain_seq}: recomputed hash does not match the stored hash — "
                    "this record's content was altered after it was written"
                ),
            )
        expected_prev = record_hash

    return ChainVerification(org_id=org_id, length=len(rows), valid=True)
