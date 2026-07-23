"""Pydantic models for the provenance index — the heart of the git-blame join."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProvenanceRecord(BaseModel):
    """One row: an AI decision that authored a specific line range in a specific commit."""

    commit_sha: str
    file_path: str
    line_start: int
    line_end: int
    decision_span_id: str
    decision_trace_id: str
    session_id: str
    reasoning_summary: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    model: str = ""
    tool: str = "claude-code"
    # A content-anchored id so a decision survives reformatting/rebase (line numbers drift).
    decision_id: str = ""
    created_at: str = Field(default_factory=_now)

    def contains_line(self, line: int) -> bool:
        return self.line_start <= line <= self.line_end


class ResolveRequest(BaseModel):
    """Ask: which AI decision authored this file:line as of this deployed commit?"""

    commit_sha: str
    file_path: str
    line: int


class ResolveResponse(BaseModel):
    """The autopsy answer: the introducing commit + the decision behind that line."""

    resolved: bool
    introducing_commit: str | None = None
    record: ProvenanceRecord | None = None
    detail: str = ""
