"""Postgres-backed provenance store — persists across Cloud Run redeploys.

Same tiny interface as the SQLite `ProvenanceStore` (see `store.py`): one table, one index.
Selected via `DATABASE_URL`; the SQLite store remains the default for local dev and tests.
"""

from __future__ import annotations

import json

import psycopg

from codeautopsy.provenance.models import IncidentRecord, ProvenanceRecord

_COLUMNS = (
    "org_id, commit_sha, file_path, line_start, line_end, decision_span_id, decision_trace_id, "
    "session_id, reasoning_summary, risk_flags, model, tool, decision_id, created_at"
)

_INCIDENT_COLUMNS = (
    "org_id, incident_id, commit_sha, file_path, line, exc_type, exc_message, resolved, "
    "decision_id, blast_radius, crash_trace_id, crash_span_id, created_at"
)

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

CREATE TABLE IF NOT EXISTS incidents (
    org_id       TEXT NOT NULL DEFAULT 'demo-public',
    incident_id  TEXT NOT NULL,
    commit_sha   TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    line         INTEGER NOT NULL,
    exc_type     TEXT NOT NULL DEFAULT '',
    exc_message  TEXT NOT NULL DEFAULT '',
    resolved     BOOLEAN NOT NULL DEFAULT FALSE,
    decision_id  TEXT,
    blast_radius INTEGER NOT NULL DEFAULT 1,
    crash_trace_id TEXT,
    crash_span_id  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_org ON incidents(org_id, created_at);
"""


class PostgresProvenanceStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._init_schema()

    def _init_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(_SCHEMA)
            # Migration for pre-tenancy databases (e.g. the live Cloud SQL instance).
            conn.execute(
                "ALTER TABLE provenance ADD COLUMN IF NOT EXISTS "
                "org_id TEXT NOT NULL DEFAULT 'demo-public'"
            )
            conn.execute("DROP INDEX IF EXISTS idx_blame")
            conn.execute(
                "CREATE INDEX idx_blame "
                "ON provenance(org_id, commit_sha, file_path, line_start, line_end)"
            )
            # Migration for incidents tables that predate the crash-trace columns.
            conn.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS crash_trace_id TEXT")
            conn.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS crash_span_id TEXT")

    def add(self, record: ProvenanceRecord) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                f"""
                INSERT INTO provenance ({_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    @staticmethod
    def _row_to_record(row: tuple, columns: list[str]) -> ProvenanceRecord:
        data = dict(zip(columns, row, strict=True))
        data["risk_flags"] = json.loads(data.get("risk_flags") or "[]")
        return ProvenanceRecord(**data)

    def find_by_line(
        self, commit_sha: str, file_path: str, line: int, org_id: str = "demo-public"
    ) -> ProvenanceRecord | None:
        """Most recent matching decision (last writer wins) if several overlap.

        Always scoped to `org_id` — a tenant can never resolve another tenant's decisions.
        """
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM provenance
                WHERE org_id = %s AND commit_sha = %s AND file_path = %s
                  AND line_start <= %s AND line_end >= %s
                ORDER BY created_at DESC
                """,
                (org_id, commit_sha, file_path, line, line),
            )
            assert cur.description is not None
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return self._row_to_record(rows[0], columns) if rows else None

    def all(self, org_id: str = "demo-public") -> list[ProvenanceRecord]:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                f"SELECT {_COLUMNS} FROM provenance WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            )
            assert cur.description is not None
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [self._row_to_record(r, columns) for r in rows]

    def count(self, org_id: str | None = None) -> int:
        with psycopg.connect(self.dsn) as conn:
            if org_id is None:
                row = conn.execute("SELECT COUNT(*) FROM provenance").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM provenance WHERE org_id = %s", (org_id,)
                ).fetchone()
        return row[0] if row else 0

    def delete(self, decision_id: str, org_id: str | None = None) -> int:
        with psycopg.connect(self.dsn) as conn:
            if org_id is None:
                cur = conn.execute("DELETE FROM provenance WHERE decision_id = %s", (decision_id,))
            else:
                cur = conn.execute(
                    "DELETE FROM provenance WHERE decision_id = %s AND org_id = %s",
                    (decision_id, org_id),
                )
            return cur.rowcount

    # --- incidents ------------------------------------------------------------------
    def add_incident(self, incident: IncidentRecord) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                f"""
                INSERT INTO incidents ({_INCIDENT_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    incident.org_id,
                    incident.incident_id,
                    incident.commit_sha,
                    incident.file_path,
                    incident.line,
                    incident.exc_type,
                    incident.exc_message,
                    incident.resolved,
                    incident.decision_id,
                    incident.blast_radius,
                    incident.crash_trace_id,
                    incident.crash_span_id,
                    incident.created_at,
                ),
            )

    def list_incidents(self, org_id: str = "demo-public") -> list[IncidentRecord]:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            )
            assert cur.description is not None
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        return [IncidentRecord(**dict(zip(columns, r, strict=True))) for r in rows]
