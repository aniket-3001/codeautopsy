"""Auto-Heal orchestration — create a run, dispatch the Fix Bot, record the outcome.

The Fix Bot needs a git working tree, `gh`, and a model key, so it can't run on the hosted
API — it runs in GitHub Actions (mirroring prognosis.yml). This module is the hosted half:
it opens the `HealRun`, fires a `repository_dispatch` to kick off the workflow, and later
receives the PR URL back. Everything degrades gracefully: with no GitHub token configured
(local dev, tests) dispatch is a no-op that still records a truthful timeline.
"""

from __future__ import annotations

import httpx

from codeautopsy.autoheal.models import (
    HealCompleteRequest,
    HealEvent,
    HealRun,
    HealRunList,
)
from codeautopsy.config import Settings, get_settings
from codeautopsy.provenance.store import ProvenanceStoreProtocol

# The sample app's seeded bug — the only thing the first-party Fix Bot ever patches. A run
# triggered without explicit coordinates targets this line (constraint D1: never customer code).
SEEDED_BUG_FILE = "codeautopsy/sample_app/main.py"
SEEDED_BUG_LINE = 91

_GITHUB_DISPATCH_URL = "https://api.github.com/repos/{repo}/dispatches"


def _event(run: HealRun, label: str, detail: str = "") -> None:
    """Append a timeline entry and bump the run's updated_at."""
    evt = HealEvent(label=label, detail=detail)
    run.events.append(evt)
    run.updated_at = evt.ts


def create_heal_run(
    store: ProvenanceStoreProtocol,
    *,
    org_id: str = "demo-public",
    commit_sha: str = "",
    file_path: str = "",
    line: int = 0,
    trigger: str = "manual",
    incident_id: str | None = None,
    settings: Settings | None = None,
) -> HealRun:
    """Open a heal run and persist it, then hand it to `dispatch_to_github`.

    Missing coordinates fall back to the seeded bug so a one-click trigger always has a real
    target. The commit defaults to whatever the caller passes; the workflow itself checks out
    the repo, so an empty commit just means "the workflow's own HEAD".
    """
    settings = settings or get_settings()
    run = HealRun(
        org_id=org_id,
        commit_sha=commit_sha,
        file_path=file_path or SEEDED_BUG_FILE,
        line=line or SEEDED_BUG_LINE,
        trigger=trigger,
        incident_id=incident_id,
    )
    _event(
        run,
        "Crash signal received",
        f"{trigger}: {run.file_path}:{run.line}",
    )
    store.save_heal_run(run)
    return dispatch_to_github(store, run, settings)


def dispatch_to_github(
    store: ProvenanceStoreProtocol, run: HealRun, settings: Settings | None = None
) -> HealRun:
    """Fire a `repository_dispatch` so the autoheal.yml workflow runs the Fix Bot.

    Graceful no-op when no token/repo is configured: the run is marked `dispatch_failed` with
    an honest timeline note rather than raising, so local demos and tests still tell the story.
    """
    settings = settings or get_settings()
    if not settings.github_token or not settings.github_repo:
        _event(
            run,
            "Fix Bot dispatch skipped",
            "no GITHUB_DISPATCH_TOKEN / CODEAUTOPSY_GITHUB_REPO configured",
        )
        run.status = "dispatch_failed"
        store.save_heal_run(run)
        return run

    callback_url = f"{settings.public_base_url.rstrip('/')}/v1/heal/{run.run_id}/complete"
    payload = {
        "event_type": "autoheal",
        "client_payload": {
            "run_id": run.run_id,
            "org_id": run.org_id,
            "commit": run.commit_sha,
            "file": run.file_path,
            "line": run.line,
            "callback_url": callback_url,
        },
    }
    url = _GITHUB_DISPATCH_URL.format(repo=settings.github_repo)
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        _event(run, "Fix Bot dispatch failed", str(exc))
        run.status = "dispatch_failed"
        store.save_heal_run(run)
        return run

    _event(
        run,
        "Fix Bot dispatched",
        f"repository_dispatch → {settings.github_repo} (event: autoheal)",
    )
    run.status = "dispatched"
    store.save_heal_run(run)
    return run


def complete_heal_run(
    store: ProvenanceStoreProtocol,
    run_id: str,
    req: HealCompleteRequest,
    org_id: str = "demo-public",
) -> HealRun | None:
    """Record the Fix Bot's outcome (called back from GitHub Actions, shared-secret authed)."""
    run = store.get_heal_run(run_id, org_id=org_id)
    if run is None:
        return None

    run.status = req.status
    run.pr_url = req.pr_url or run.pr_url
    run.branch = req.branch or run.branch
    run.explanation = req.explanation or run.explanation
    run.lesson = req.lesson or run.lesson
    run.detail = req.detail or run.detail

    if req.status == "succeeded":
        _event(run, "Fix verified & PR opened", req.pr_url or req.explanation or "")
    elif req.status == "failed":
        _event(run, "Fix Bot could not verify a fix", req.detail or req.explanation or "")
    else:
        _event(run, f"Fix Bot: {req.status}", req.detail)

    store.save_heal_run(run)
    return run


def list_heal_runs(
    store: ProvenanceStoreProtocol, org_id: str = "demo-public"
) -> HealRunList:
    """Every heal run for this org, newest first — what the #/autoheal page polls."""
    runs = store.list_heal_runs(org_id=org_id)
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return HealRunList(runs=runs)
