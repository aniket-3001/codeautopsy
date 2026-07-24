"""Pure tool logic for the CodeAutopsy MCP server — no `mcp` package import, so it is
unit-testable on its own. `server.py` wraps these in FastMCP tools.

Each function returns a plain JSON-serialisable dict: MCP tool results and test assertions
read the same shape.
"""

from __future__ import annotations

from pathlib import Path

from codeautopsy.config import Settings, get_settings
from codeautopsy.provenance.indexer import resolve as resolve_provenance
from codeautopsy.provenance.models import ResolveRequest, ResolveResponse
from codeautopsy.provenance.store import ProvenanceStore, ProvenanceStoreProtocol
from codeautopsy.reliability.core import compute_leaderboard, score_snippet


def make_store(settings: Settings) -> ProvenanceStoreProtocol:
    """Local-first store, mirroring the provenance service: Postgres when DATABASE_URL is set,
    else the on-disk SQLite the CLI and recorder already use — so an IDE-side MCP client reads
    the developer's *own* provenance index without a network hop."""
    if settings.database_url:
        from codeautopsy.provenance.store_postgres import PostgresProvenanceStore

        return PostgresProvenanceStore(settings.database_url)
    return ProvenanceStore(settings.provenance_db)


def _autopsy_payload(
    resp: ResolveResponse, commit_sha: str, file_path: str, line: int
) -> dict:
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
) -> dict:
    """Resolve a crash coordinate to the AI decision that authored the line.

    Blames `file_path:line` at the deployed `commit_sha` back to its introducing commit, then
    returns the recorded AI decision (reasoning, tool/model, risk flags) for that line range.
    """
    settings = settings or get_settings()
    store = store or make_store(settings)
    repo_path: str | Path | None = repo if repo is not None else settings.target_repo
    resp = resolve_provenance(
        store,
        ResolveRequest(commit_sha=commit_sha, file_path=file_path, line=line),
        repo=repo_path,
        org_id=org_id,
    )
    return _autopsy_payload(resp, commit_sha, file_path, line)


def prognose(
    code: str,
    reasoning: str = "",
    *,
    org_id: str = "demo-public",
    store: ProvenanceStoreProtocol | None = None,
    settings: Settings | None = None,
) -> dict:
    """Price a snippet's risk against this project's real production crash history."""
    settings = settings or get_settings()
    store = store or make_store(settings)
    return score_snippet(store, code, reasoning, org_id=org_id).model_dump()


def leaderboard(
    *,
    org_id: str = "demo-public",
    store: ProvenanceStoreProtocol | None = None,
    settings: Settings | None = None,
) -> dict:
    """Rank the AI tools/models used in this project by real production crash rate."""
    settings = settings or get_settings()
    store = store or make_store(settings)
    return compute_leaderboard(store, org_id=org_id).model_dump()
