"""Pydantic models for the Reliability lens — leaderboard rows and risk-gate verdicts.

Crash rates are stored as plain fields (computed once in `core.py`), not `@property`, so
they serialize into the JSON the dashboard SPA reads. FlagStats (prognosis) keeps crash_rate
as a property because it's only ever used server-side; here it crosses the wire.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelScore(BaseModel):
    """One row of the reliability leaderboard: a single AI tool/model's track record."""

    tool: str = "unknown"
    model: str = "unknown"
    decisions: int = 0
    crashed_decisions: int = 0
    # Total production incidents traced back to any of this tool/model's decisions.
    incidents_caused: int = 0
    # Fraction of this tool/model's decisions that went on to crash in production.
    crash_rate: float | None = None
    # The single risk flag with the worst crash rate among this tool/model's decisions.
    worst_flag: str = ""
    worst_flag_rate: float | None = None


class LeaderboardReport(BaseModel):
    """The whole board, already ranked worst-first."""

    org_id: str
    scores: list[ModelScore] = Field(default_factory=list)
    total_decisions: int = 0
    total_incidents: int = 0


class RiskGateRequest(BaseModel):
    """Paste a snippet (and optionally the reasoning behind it) and get it priced."""

    code: str = ""
    reasoning: str = ""


class RiskGateFlag(BaseModel):
    """One risk flag the snippet tripped, priced against this org's history."""

    flag: str
    # None when fewer than min_samples historical decisions carry this flag — unpriced.
    crash_rate: float | None = None
    sample_size: int = 0


class RiskGateResponse(BaseModel):
    """The verdict: which flags fired, and the worst priced crash rate among them."""

    flags: list[RiskGateFlag] = Field(default_factory=list)
    worst_flag: str = ""
    crash_rate: float | None = None
    sample_size: int = 0
    # "clear"   — no risk flags tripped at all.
    # "flagged" — flags tripped, but none has enough history to price yet.
    # "priced"  — at least one flag has a historical crash rate we can put a number on.
    verdict: str = "clear"
