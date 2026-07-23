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
        self, commit_sha: str, file_path: str, line: int
    ) -> ProvenanceRecord | None: ...
    def all(self) -> list[ProvenanceRecord]: ...
    def count(self) -> int: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
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
CREATE INDEX IF NOT EXISTS idx_blame
    ON provenance(commit_sha, file_path, line_start, line_end);
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

    # --- writes -------------------------------------------------------------------
    def add(self, record: ProvenanceRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO provenance (
                    commit_sha, file_path, line_start, line_end,
                    decision_span_id, decision_trace_id, session_id,
                    reasoning_summary, risk_flags, model, tool, decision_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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

    def find_by_line(self, commit_sha: str, file_path: str, line: int) -> ProvenanceRecord | None:
        """The core lookup: which decision's range contains this line at this commit?

        Returns the most recent matching decision (last writer wins) if several overlap.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM provenance
                WHERE commit_sha = ? AND file_path = ?
                  AND line_start <= ? AND line_end >= ?
                ORDER BY created_at DESC
                """,
                (commit_sha, file_path, line, line),
            ).fetchall()
        return self._row_to_record(rows[0]) if rows else None

    def all(self) -> list[ProvenanceRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM provenance ORDER BY created_at").fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM provenance").fetchone()[0]
