"""SQLite-backed provenance store.

Deliberately tiny: one table, one index. The whole product's data side is a lookup of
`(commit, file, line) -> decision`. Keeping it boring keeps it reliable for the demo.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from codeautopsy.provenance.models import ProvenanceRecord


class ProvenanceStoreProtocol(Protocol):
    """The tiny interface both the SQLite and Postgres backends satisfy."""

    def add(self, record: ProvenanceRecord) -> None: ...
    def add_many(self, records: list[ProvenanceRecord]) -> int: ...
    def find_by_line(
        self, commit_sha: str, file_path: str, line: int, org_id: str = "demo-public"
    ) -> ProvenanceRecord | None: ...
    def all(self, org_id: str = "demo-public") -> list[ProvenanceRecord]: ...
    def count(self, org_id: str | None = None) -> int: ...
    def delete(self, decision_id: str, org_id: str | None = None) -> int: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
    org_id            TEXT NOT NULL DEFAULT 'demo-public',
    commit_sha        TEXT NOT NULL,
    file_path         TEXT NOT NULL,
    line_start        INTEGER NOT NULL,
    line_end          INTEGER NOT NULL,
    decision_span_id  TEXT NOT NULL,
    decision_trace_id TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    reasoning_summary TEXT NOT NULL DEFAULT '',
    risk_flags        TEXT NOT NULL DEFAULT '[]',
    model             TEXT NOT NULL DEFAULT '',
    tool              TEXT NOT NULL DEFAULT 'claude-code',
    decision_id       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file ON provenance(file_path);
"""


class ProvenanceStore:
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
            # Migration for pre-tenancy databases: add org_id if this table predates it, then
            # rebuild idx_blame to include it (SQLite has no "ADD COLUMN IF NOT EXISTS").
            try:
                conn.execute(
                    "ALTER TABLE provenance ADD COLUMN org_id TEXT NOT NULL DEFAULT 'demo-public'"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute("DROP INDEX IF EXISTS idx_blame")
            conn.execute(
                "CREATE INDEX idx_blame "
                "ON provenance(org_id, commit_sha, file_path, line_start, line_end)"
            )

    # --- writes -------------------------------------------------------------------
    def add(self, record: ProvenanceRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO provenance (
                    org_id, commit_sha, file_path, line_start, line_end,
                    decision_span_id, decision_trace_id, session_id,
                    reasoning_summary, risk_flags, model, tool, decision_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.org_id,
                    record.commit_sha,
                    record.file_path,
                    record.line_start,
                    record.line_end,
                    record.decision_span_id,
                    record.decision_trace_id,
                    record.session_id,
                    record.reasoning_summary,
                    json.dumps(record.risk_flags),
                    record.model,
                    record.tool,
                    record.decision_id,
                    record.created_at,
                ),
            )

    def add_many(self, records: list[ProvenanceRecord]) -> int:
        for r in records:
            self.add(r)
        return len(records)

    # --- reads --------------------------------------------------------------------
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProvenanceRecord:
        data = dict(row)
        data["risk_flags"] = json.loads(data.get("risk_flags") or "[]")
        return ProvenanceRecord(**data)

    def find_by_line(
        self, commit_sha: str, file_path: str, line: int, org_id: str = "demo-public"
    ) -> ProvenanceRecord | None:
        """The core lookup: which decision's range contains this line at this commit?

        Returns the most recent matching decision (last writer wins) if several overlap.
        Always scoped to `org_id` — a tenant can never resolve another tenant's decisions.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM provenance
                WHERE org_id = ? AND commit_sha = ? AND file_path = ?
                  AND line_start <= ? AND line_end >= ?
                ORDER BY created_at DESC
                """,
                (org_id, commit_sha, file_path, line, line),
            ).fetchall()
        return self._row_to_record(rows[0]) if rows else None

    def all(self, org_id: str = "demo-public") -> list[ProvenanceRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM provenance WHERE org_id = ? ORDER BY created_at", (org_id,)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self, org_id: str | None = None) -> int:
        with self._conn() as conn:
            if org_id is None:
                return conn.execute("SELECT COUNT(*) FROM provenance").fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM provenance WHERE org_id = ?", (org_id,)
            ).fetchone()[0]

    def delete(self, decision_id: str, org_id: str | None = None) -> int:
        with self._conn() as conn:
            if org_id is None:
                cur = conn.execute("DELETE FROM provenance WHERE decision_id = ?", (decision_id,))
            else:
                cur = conn.execute(
                    "DELETE FROM provenance WHERE decision_id = ? AND org_id = ?",
                    (decision_id, org_id),
                )
            return cur.rowcount
