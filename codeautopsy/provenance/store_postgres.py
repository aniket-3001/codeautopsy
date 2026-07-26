"""Postgres-backed provenance store — persists across Cloud Run redeploys.

Same tiny interface as the SQLite `ProvenanceStore` (see `store.py`): one table, one index.
Selected via `DATABASE_URL`; the SQLite store remains the default for local dev and tests.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from codeautopsy.autoheal.models import HealRun
from codeautopsy.provenance.integrity import (
    GENESIS_HASH,
    ChainRow,
    ChainVerification,
    compute_hash,
    verify_chain,
)
from codeautopsy.provenance.models import IncidentRecord, LessonRecord, ProvenanceRecord

_COLUMNS = (
    "org_id, commit_sha, file_path, line_start, line_end, decision_span_id, decision_trace_id, "
    "session_id, reasoning_summary, risk_flags, risk_source, model, tool, decision_id, created_at"
)

_INCIDENT_COLUMNS = (
    "org_id, incident_id, commit_sha, file_path, line, exc_type, exc_message, resolved, "
    "decision_id, blast_radius, crash_trace_id, crash_span_id, ci_run_url, created_at"
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
    risk_source       TEXT NOT NULL DEFAULT 'heuristic',
    model             TEXT NOT NULL DEFAULT '',
    tool              TEXT NOT NULL DEFAULT 'claude-code',
    decision_id       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    record_hash       TEXT NOT NULL DEFAULT '',
    prev_hash         TEXT NOT NULL DEFAULT '',
    chain_seq         INTEGER NOT NULL DEFAULT 0
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
    ci_run_url   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_org ON incidents(org_id, created_at);

CREATE TABLE IF NOT EXISTS heal_runs (
    org_id     TEXT NOT NULL DEFAULT 'demo-public',
    run_id     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'triggered',
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_heal_org ON heal_runs(org_id, created_at);

CREATE TABLE IF NOT EXISTS lessons (
    org_id         TEXT NOT NULL DEFAULT 'demo-public',
    fingerprint    TEXT NOT NULL,
    lesson         TEXT NOT NULL,
    decision_id    TEXT NOT NULL DEFAULT '',
    file_path      TEXT NOT NULL DEFAULT '',
    cause_of_death TEXT NOT NULL DEFAULT '',
    patch_summary  TEXT NOT NULL DEFAULT '',
    times_seen     INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (org_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_lessons_org ON lessons(org_id, times_seen);
"""

_LESSON_COLUMNS = (
    "org_id, fingerprint, lesson, decision_id, file_path, cause_of_death, "
    "patch_summary, times_seen, created_at, updated_at"
)


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
            # Migration for provenance tables that predate the tamper-evidence hash chain.
            # Pre-existing rows keep chain_seq = 0 (excluded from the chain, not backfilled —
            # there is no previous hash to anchor them to).
            conn.execute(
                "ALTER TABLE provenance ADD COLUMN IF NOT EXISTS "
                "record_hash TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE provenance ADD COLUMN IF NOT EXISTS "
                "prev_hash TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE provenance ADD COLUMN IF NOT EXISTS "
                "chain_seq INTEGER NOT NULL DEFAULT 0"
            )
            # Migration for provenance tables that predate mandatory risk-source labeling.
            conn.execute(
                "ALTER TABLE provenance ADD COLUMN IF NOT EXISTS "
                "risk_source TEXT NOT NULL DEFAULT 'heuristic'"
            )
            conn.execute("DROP INDEX IF EXISTS idx_blame")
            conn.execute(
                "CREATE INDEX idx_blame "
                "ON provenance(org_id, commit_sha, file_path, line_start, line_end)"
            )
            # Migration for incidents tables that predate the crash-trace columns.
            conn.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS crash_trace_id TEXT")
            conn.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS crash_span_id TEXT")
            conn.execute(
                "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS ci_run_url "
                "TEXT NOT NULL DEFAULT ''"
            )

    def _chain_tip(self, conn: psycopg.Connection[Any], org_id: str) -> tuple[str, int]:
        """The `(record_hash, chain_seq)` of this org's most recently written record, or the
        genesis pair if the org has no chained records yet."""
        row = conn.execute(
            "SELECT record_hash, chain_seq FROM provenance WHERE org_id = %s "
            "ORDER BY chain_seq DESC LIMIT 1",
            (org_id,),
        ).fetchone()
        if row is None or not row[0]:
            return GENESIS_HASH, 0
        return str(row[0]), int(row[1])

    def add(self, record: ProvenanceRecord) -> None:
        with psycopg.connect(self.dsn) as conn:
            prev_hash, prev_seq = self._chain_tip(conn, record.org_id)
            record_hash = compute_hash(prev_hash, record)
            conn.execute(
                f"""
                INSERT INTO provenance (
                    {_COLUMNS}, record_hash, prev_hash, chain_seq
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    record.risk_source,
                    record.model,
                    record.tool,
                    record.decision_id,
                    record.created_at,
                    record_hash,
                    prev_hash,
                    prev_seq + 1,
                ),
            )

    def add_many(self, records: list[ProvenanceRecord]) -> int:
        for r in records:
            self.add(r)
        return len(records)

    @staticmethod
    def _row_to_record(row: tuple[Any, ...], columns: list[str]) -> ProvenanceRecord:
        data = dict(zip(columns, row, strict=True))
        data["risk_flags"] = json.loads(data.get("risk_flags") or "[]")
        # Hash-chain bookkeeping columns aren't part of the record's own content.
        data.pop("record_hash", None)
        data.pop("prev_hash", None)
        data.pop("chain_seq", None)
        return ProvenanceRecord(**data)

    def chain_rows(self, org_id: str = "demo-public") -> list[ChainRow]:
        """(record, prev_hash, record_hash, chain_seq) for this org's chained records, in
        chain order. Rows with chain_seq == 0 predate the hash chain and are excluded."""
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                f"SELECT {_COLUMNS}, record_hash, prev_hash, chain_seq FROM provenance "
                "WHERE org_id = %s AND chain_seq > 0 ORDER BY chain_seq",
                (org_id,),
            )
            assert cur.description is not None
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()
        result: list[ChainRow] = []
        for row in rows:
            data = dict(zip(columns, row, strict=True))
            prev_hash = str(data.pop("prev_hash", "") or "")
            record_hash = str(data.pop("record_hash", "") or "")
            chain_seq = int(data.pop("chain_seq", 0) or 0)
            data["risk_flags"] = json.loads(data.get("risk_flags") or "[]")
            result.append((ProvenanceRecord(**data), prev_hash, record_hash, chain_seq))
        return result

    def verify_integrity(self, org_id: str = "demo-public") -> ChainVerification:
        """Recompute this org's hash chain and compare it against what's stored — a mismatch
        means a provenance row was altered after it was written."""
        return verify_chain(self.chain_rows(org_id), org_id=org_id)

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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    incident.ci_run_url,
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

    # --- heal runs ------------------------------------------------------------------
    def save_heal_run(self, run: HealRun) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                """
                INSERT INTO heal_runs (org_id, run_id, status, data, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    run.org_id,
                    run.run_id,
                    run.status,
                    run.model_dump_json(),
                    run.created_at,
                    run.updated_at,
                ),
            )

    def get_heal_run(self, run_id: str, org_id: str = "demo-public") -> HealRun | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT data FROM heal_runs WHERE org_id = %s AND run_id = %s",
                (org_id, run_id),
            ).fetchone()
        return HealRun.model_validate_json(row[0]) if row else None

    def list_heal_runs(self, org_id: str = "demo-public") -> list[HealRun]:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                "SELECT data FROM heal_runs WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            )
            rows = cur.fetchall()
        return [HealRun.model_validate_json(r[0]) for r in rows]

    def record_lesson(self, lesson: LessonRecord) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                f"""
                INSERT INTO lessons ({_LESSON_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, fingerprint) DO UPDATE SET
                    lesson = EXCLUDED.lesson,
                    decision_id = EXCLUDED.decision_id,
                    patch_summary = EXCLUDED.patch_summary,
                    times_seen = lessons.times_seen + 1,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    lesson.org_id,
                    lesson.fingerprint,
                    lesson.lesson,
                    lesson.decision_id,
                    lesson.file_path,
                    lesson.cause_of_death,
                    lesson.patch_summary,
                    lesson.times_seen,
                    lesson.created_at,
                    lesson.updated_at,
                ),
            )

    def find_lesson(self, org_id: str, fingerprint: str) -> LessonRecord | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                f"SELECT {_LESSON_COLUMNS} FROM lessons WHERE org_id = %s AND fingerprint = %s",
                (org_id, fingerprint),
            ).fetchone()
        return self._lesson_from_row(row) if row else None

    def list_lessons(self, org_id: str = "demo-public") -> list[LessonRecord]:
        with psycopg.connect(self.dsn) as conn:
            cur = conn.execute(
                f"SELECT {_LESSON_COLUMNS} FROM lessons WHERE org_id = %s "
                "ORDER BY times_seen DESC, updated_at DESC",
                (org_id,),
            )
            rows = cur.fetchall()
        return [self._lesson_from_row(r) for r in rows]

    @staticmethod
    def _lesson_from_row(row: tuple[Any, ...]) -> LessonRecord:
        return LessonRecord(
            org_id=row[0],
            fingerprint=row[1],
            lesson=row[2],
            decision_id=row[3],
            file_path=row[4],
            cause_of_death=row[5],
            patch_summary=row[6],
            times_seen=row[7],
            created_at=row[8],
            updated_at=row[9],
        )
