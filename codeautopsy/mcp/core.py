"""Pure tool logic for the CodeAutopsy MCP server — no `mcp` package import, so it is
unit-testable on its own. `server.py` wraps these in FastMCP tools.

Each function returns a plain JSON-serialisable dict: MCP tool results and test assertions
read the same shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from codeautopsy.config import Settings, get_settings
from codeautopsy.fixbot.core import build_genealogy
from codeautopsy.fixbot.lessons import recall_lesson
from codeautopsy.postmortem.core import render_postmortem
from codeautopsy.provenance.indexer import resolve as resolve_provenance
from codeautopsy.provenance.models import ResolveRequest, ResolveResponse
from codeautopsy.provenance.store import ProvenanceStoreProtocol, make_store
from codeautopsy.reliability.core import compute_leaderboard, score_snippet

_tracer_name = "codeautopsy.mcp"


def _autopsy_payload(
    resp: ResolveResponse, commit_sha: str, file_path: str, line: int
) -> dict[str, Any]:
    coordinate = f"{file_path}:{line}@{commit_sha[:12]}"
    if not resp.resolved or resp.record is None:
        return {
            "resolved": False,
            "coordinate": coordinate,
            "introducing_commit": resp.introducing_commit,
            "detail": resp.detail,
        }
    rec = resp.record
    return {
        "resolved": True,
        "coordinate": coordinate,
        "introducing_commit": resp.introducing_commit,
        "decision_id": rec.decision_id,
        "authored_by": {"tool": rec.tool, "model": rec.model},
        "reasoning_summary": rec.reasoning_summary,
        "risk_flags": rec.risk_flags,
        "line_range": [rec.line_start, rec.line_end],
        "decision_trace_id": rec.decision_trace_id,
        "decision_span_id": rec.decision_span_id,
        "confidence": resp.confidence,
        "confidence_factors": resp.confidence_factors,
        "detail": resp.detail,
    }


def autopsy(
    commit_sha: str,
    file_path: str,
    line: int,
    *,
    repo: str | None = None,
    org_id: str = "demo-public",
    store: ProvenanceStoreProtocol | None = None,
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
) -> dict[str, Any]:
    """Resolve a crash coordinate to the AI decision that authored the line.

    Blames `file_path:line` at the deployed `commit_sha` back to its introducing commit, then
    returns the recorded AI decision (reasoning, tool/model, risk flags) for that line range.

    Every call is a span, `tracer_provider` injectable for tests — dogfooding: the tool
    CodeAutopsy hands other agents is itself observed by the same SigNoz pipeline it
    instruments everything else with. An MCP tool call can go quiet in two different ways —
    it can raise, or it can return a perfectly well-formed result that just didn't find
    anything — and only the first one shows up if you only wrap the call in a try/except.
    The span attribute below makes the second kind ("resolved": false) queryable too,
    without misrepresenting a legitimate "no match" as an error.
    """
    tracer = trace.get_tracer(_tracer_name, tracer_provider=tracer_provider)
    with tracer.start_as_current_span("codeautopsy.mcp.autopsy") as span:
        span.set_attribute("code.filepath", file_path)
        span.set_attribute("code.lineno", line)
        span.set_attribute("codeautopsy.commit_sha", commit_sha[:12])
        try:
            settings = settings or get_settings()
            store = store or make_store(settings)
            repo_path: str | Path | None = repo if repo is not None else settings.target_repo
            resp = resolve_provenance(
                store,
                ResolveRequest(commit_sha=commit_sha, file_path=file_path, line=line),
                repo=repo_path,
                org_id=org_id,
            )
            payload = _autopsy_payload(resp, commit_sha, file_path, line)
            span.set_attribute("codeautopsy.mcp.resolved", payload["resolved"])
            return payload
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def prognose(
    code: str,
    reasoning: str = "",
    *,
    org_id: str = "demo-public",
    store: ProvenanceStoreProtocol | None = None,
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
) -> dict[str, Any]:
    """Price a snippet's risk against this project's real production crash history."""
    tracer = trace.get_tracer(_tracer_name, tracer_provider=tracer_provider)
    with tracer.start_as_current_span("codeautopsy.mcp.prognose") as span:
        span.set_attribute("codeautopsy.mcp.snippet_length", len(code))
        try:
            settings = settings or get_settings()
            store = store or make_store(settings)
            result = score_snippet(store, code, reasoning, org_id=org_id).model_dump()
            span.set_attribute("codeautopsy.mcp.verdict", result["verdict"])
            return result
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def postmortem(
    commit_sha: str,
    file_path: str,
    line: int,
    *,
    org_id: str = "demo-public",
    store: ProvenanceStoreProtocol | None = None,
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
) -> dict[str, Any]:
    """Render the full chain-of-custody postmortem for a crash, as shareable markdown.

    The same assembly `codeautopsy report` prints on the CLI: crash -> cause of death ->
    blame -> decision -> reasoning -> confidence -> lesson learned (if this class of bug has
    struck before) — everything CodeAutopsy knows about the incident, in one document an agent
    can paste straight into a PR description or incident channel.
    """
    tracer = trace.get_tracer(_tracer_name, tracer_provider=tracer_provider)
    with tracer.start_as_current_span("codeautopsy.mcp.postmortem") as span:
        span.set_attribute("code.filepath", file_path)
        span.set_attribute("code.lineno", line)
        span.set_attribute("codeautopsy.commit_sha", commit_sha[:12])
        try:
            settings = settings or get_settings()
            store = store or make_store(settings)
            genealogy = build_genealogy(settings, commit_sha, file_path, line)
            lesson_hit = recall_lesson(
                store,
                cause_of_death=genealogy.cause_of_death,
                file_path=file_path,
                risk_flags=genealogy.risk_flags,
                org_id=org_id,
            )
            markdown = render_postmortem(
                genealogy, lesson=lesson_hit, ci_run_url=settings.ci_run_url
            )
            span.set_attribute("codeautopsy.mcp.lesson_recalled", lesson_hit is not None)
            return {"markdown": markdown}
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def leaderboard(
    *,
    org_id: str = "demo-public",
    store: ProvenanceStoreProtocol | None = None,
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
) -> dict[str, Any]:
    """Rank the AI tools/models used in this project by real production crash rate."""
    tracer = trace.get_tracer(_tracer_name, tracer_provider=tracer_provider)
    with tracer.start_as_current_span("codeautopsy.mcp.leaderboard") as span:
        try:
            settings = settings or get_settings()
            store = store or make_store(settings)
            result = compute_leaderboard(store, org_id=org_id).model_dump()
            span.set_attribute("codeautopsy.mcp.total_decisions", result["total_decisions"])
            span.set_attribute("codeautopsy.mcp.total_incidents", result["total_incidents"])
            return result
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
