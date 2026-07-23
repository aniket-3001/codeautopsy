"""Pydantic models for the Fix Bot: the genealogy fed to the agent, and what comes back."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Genealogy(BaseModel):
    """Everything the Fix Bot needs to hand the agent its own mistake: why the line was
    written (from the provenance record) and what actually broke it (from the incident log).
    """

    file_path: str
    line: int
    commit_sha: str
    file_content: str
    reasoning_summary: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    decision_id: str = ""
    exc_type: str = ""
    exc_message: str = ""
    cause_of_death: str = ""
    context: dict = Field(default_factory=dict)


class FixProposal(BaseModel):
    """Structured output the model must return — enforced via tool-use, not prose parsing."""

    explanation: str
    fixed_file_content: str
    regression_test_code: str
    lesson: str = ""


class FixBotResult(BaseModel):
    """What `codeautopsy fix` reports back."""

    verified: bool
    explanation: str = ""
    lesson: str = ""
    test_output: str = ""
    branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    detail: str = ""
