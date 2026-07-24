"""Pydantic models for the Auto-Heal loop: a heal run and its live timeline."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

# The lifecycle a heal run walks through. Kept small and linear so the UI can render it as a
# checklist: triggered -> dispatched -> (Fix Bot runs in GitHub Actions) -> succeeded | failed.
# `dispatch_failed` is the graceful branch when no GitHub token is configured locally.
HealStatus = str  # "triggered" | "dispatched" | "dispatch_failed" | "succeeded" | "failed"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_id() -> str:
    return f"heal_{uuid4().hex[:12]}"


class HealEvent(BaseModel):
    """One entry in a heal run's timeline — what happened and when."""

    ts: str = Field(default_factory=_now)
    label: str
    detail: str = ""


class HealRun(BaseModel):
    """One attempt to auto-heal a crash: from the triggering signal to the opened PR.

    Persisted per-org so a signed-in judge sees only their own runs. `events` is the live
    timeline the #/autoheal page renders and polls.
    """

    org_id: str = "demo-public"
    run_id: str = Field(default_factory=_run_id)
    status: HealStatus = "triggered"
    # What broke and where — the coordinates handed to `codeautopsy fix`.
    commit_sha: str
    file_path: str
    line: int
    # How this run began: "manual" (the dashboard button) or "signoz-alert" (the webhook).
    trigger: str = "manual"
    incident_id: str | None = None
    # Filled in when the Fix Bot reports back via /v1/heal/{run_id}/complete.
    pr_url: str | None = None
    branch: str | None = None
    explanation: str = ""
    lesson: str = ""
    detail: str = ""
    events: list[HealEvent] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class HealTriggerRequest(BaseModel):
    """Manual trigger from the dashboard button. Coordinates default to the sample app's
    seeded bug so a judge can fire a run with one click, but may be overridden."""

    commit_sha: str = ""
    file_path: str = ""
    line: int = 0
    incident_id: str | None = None


class HealWebhookRequest(BaseModel):
    """The SigNoz alert webhook payload we care about. SigNoz posts a rich alert body; we
    only need enough to know a crash storm fired. Coordinates fall back to the seeded bug."""

    org_id: str = "demo-public"
    commit_sha: str = ""
    file_path: str = ""
    line: int = 0
    alert: str = ""


class HealCompleteRequest(BaseModel):
    """The Fix Bot's report-back from GitHub Actions (shared-secret authed)."""

    org_id: str = "demo-public"
    status: HealStatus
    pr_url: str | None = None
    branch: str | None = None
    explanation: str = ""
    lesson: str = ""
    detail: str = ""


class HealRunList(BaseModel):
    """Response for GET /v1/heal/runs — newest first."""

    runs: list[HealRun] = Field(default_factory=list)
