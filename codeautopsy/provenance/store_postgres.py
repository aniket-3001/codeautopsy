"""Postgres-backed provenance store — persists across Cloud Run redeploys.

Same tiny interface as the SQLite `ProvenanceStore` (see `store.py`): one table, one index.
Selected via `DATABASE_URL`; the SQLite store remains the default for local dev and tests.
"""

from __future__ import annotations

import json

import psycopg

from codeautopsy.provenance.models import ProvenanceRecord

_COLUMNS = (
    "commit_sha, file_path, line_start, line_end, decision_span_id, decision_trace_id, "
    "session_id, reasoning_summary, risk_flags, model, tool, decision_id, created_at"
)

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


class PostgresProvenanceStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._init_schema()

    def _init_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(_SCHEMA)

    def add(self, record: ProvenanceRecord) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                f"""
                INSERT INTO provenance ({_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    @staticmethod
    def _row_to_record(row: tuple, columns: list[str]) -> ProvenanceRecord:
        data = dict(zip(columns, row, strict=True))
        data["risk_flags"] = json.loads(data.get("risk_flags") or "[]")
        return ProvenanceRecord(**data)

    def find_by_line(self, commit_sha: str, file_path: str, line: int) -> ProvenanceRecord | None:
        """Most recent matching decision (last writer wins) if several overlap."""
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM provenance
                WHERE commit_sha = %s AND file_path = %s
                  AND line_start <= %s AND line_end >= %s
                ORDER BY created_at DESC
                """,
                (commit_sha, file_path, line, line),
            )
            assert cur.description is not None
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return self._row_to_record(rows[0], columns) if rows else None

    def all(self) -> list[ProvenanceRecord]:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(f"SELECT {_COLUMNS} FROM provenance ORDER BY created_at")
            assert cur.description is not None
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [self._row_to_record(r, columns) for r in rows]

    def count(self) -> int:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT COUNT(*) FROM provenance").fetchone()
        return row[0] if row else 0

    def delete(self, decision_id: str) -> int:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute("DELETE FROM provenance WHERE decision_id = %s", (decision_id,))
            return cur.rowcount
