"""Pydantic models for Prognosis — the pre-mortem PR bot.

Autopsy resolves a crash back to the decision that caused it, after the fact. Prognosis
runs the same git-blame join *before* merge, against a PR's diff, and prices each line's
risk flags against the track record every other decision carrying that flag has already
built up in production.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FlagStats(BaseModel):
    """Track record for one risk flag, across every decision + incident ever indexed."""

    flag: str
    decisions: int = 0
    crashed_decisions: int = 0

    @property
    def crash_rate(self) -> float | None:
        if self.decisions == 0:
            return None
        return self.crashed_decisions / self.decisions


class LineFinding(BaseModel):
    """One flagged line in the diff."""

    file_path: str
    line: int
    risk_flags: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    decision_id: str = ""
    # "decision": resolved to a recorded AI decision via the blame join.
    # "pattern": no decision indexed for this line — flagged by scanning its raw text instead.
    source: str = "decision"
    # Highest crash rate among this line's flags that clear min_samples; None if none do.
    crash_rate: float | None = None
    worst_flag: str = ""
    sample_size: int = 0


class PrognosisReport(BaseModel):
    base_ref: str
    head_ref: str
    lines_scanned: int = 0
    findings: list[LineFinding] = Field(default_factory=list)
    flag_stats: dict[str, FlagStats] = Field(default_factory=dict)
