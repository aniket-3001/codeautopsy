"""Incident log — the crash-time half of what the Fix Bot needs.

The provenance record answers "which AI decision wrote this line and why." An incident
record answers "what input actually broke it." Together they're the genealogy the Fix Bot
feeds back to the agent. Stored the same way as the recorder's pending queue: append-only
JSONL under `.codeautopsy/`, so no extra service is needed to read it back.
"""

from __future__ import annotations

import json
from pathlib import Path

from codeautopsy.provenance.models import ProvenanceRecord


def _codeautopsy_dir(repo_root: Path) -> Path:
    d = repo_root / ".codeautopsy"
    d.mkdir(exist_ok=True)
    return d


def incidents_path(repo_root: Path) -> Path:
    return _codeautopsy_dir(repo_root) / "incidents.jsonl"


def append_incident(repo_root: Path, record: dict) -> None:
    with incidents_path(repo_root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_incidents(repo_root: Path) -> list[dict]:
    path = incidents_path(repo_root)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def latest_incident_for(repo_root: Path, file_path: str, line: int) -> dict | None:
    """Most recent incident whose crash line falls within this file, closest first.

    Exact line match is preferred; if the crash line was reported slightly differently
    (e.g. a wrapper frame), fall back to the most recent incident in the same file.
    """
    matches = [i for i in read_incidents(repo_root) if i.get("file_path") == file_path]
    if not matches:
        return None
    exact = [i for i in matches if i.get("line") == line]
    return (exact or matches)[-1]


def record_incident(
    repo_root: Path,
    *,
    file_path: str,
    line: int,
    exc_type: str,
    exc_message: str,
    cause_of_death: str,
    resolved: bool,
    provenance: ProvenanceRecord | None,
    context: dict | None,
    blast_radius: int,
) -> None:
    """Best-effort incident append. Must never raise — called from a live exception path."""
    try:
        append_incident(
            repo_root,
            {
                "file_path": file_path,
                "line": line,
                "exc_type": exc_type,
                "exc_message": exc_message,
                "cause_of_death": cause_of_death,
                "resolved": resolved,
                "decision_id": provenance.decision_id if provenance else None,
                "reasoning_summary": provenance.reasoning_summary if provenance else "",
                "risk_flags": provenance.risk_flags if provenance else [],
                "commit_sha": provenance.commit_sha if provenance else None,
                "context": context or {},
                "blast_radius": blast_radius,
            },
        )
    except OSError:
        pass
