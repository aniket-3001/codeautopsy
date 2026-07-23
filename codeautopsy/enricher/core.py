"""Autopsy Enricher — the join engine's runtime half.

On a recorded exception, resolves the crashing file:line against the provenance service
and mints a linked `codeautopsy.autopsy` child span carrying an OTel span link back to the
AI decision span that authored the line. This is the exact mechanism validated in the
Day-0 smoke test (see scripts/day0_smoke.py) — here it runs against a *real* exception
instead of a hand-constructed one.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from codeautopsy.config import Settings, get_settings
from codeautopsy.enricher.incidents import record_incident
from codeautopsy.provenance.models import ResolveRequest, ResolveResponse

CAUSE_OF_DEATH_BY_EXC: dict[str, str] = {
    "ValueError": "invalid value — unvalidated input",
    "TypeError": "type mismatch — an unchecked assumption about input shape",
    "KeyError": "missing key — unvalidated dict access",
    "AttributeError": "attribute access on an unexpected None/type",
    "IndexError": "out-of-range access — unvalidated collection bounds",
    "ZeroDivisionError": "division by zero — unvalidated denominator",
}


def resolve_decision(
    settings: Settings, commit_sha: str, file_path: str, line: int
) -> ResolveResponse:
    """Ask the provenance service which AI decision authored this crashing line."""
    req = ResolveRequest(commit_sha=commit_sha, file_path=file_path, line=line)
    try:
        resp = httpx.post(f"{settings.provenance_url}/resolve", json=req.model_dump(), timeout=3.0)
        resp.raise_for_status()
        return ResolveResponse(**resp.json())
    except httpx.HTTPError as exc:
        return ResolveResponse(resolved=False, detail=f"provenance service unreachable: {exc}")


def _decision_link(record) -> Link | None:
    """Build the OTel span link that jumps from this crash to the AI's decision span."""
    try:
        target = SpanContext(
            trace_id=int(record.decision_trace_id, 16),
            span_id=int(record.decision_span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except (ValueError, TypeError):
        return None
    return Link(
        target,
        attributes={
            "codeautopsy.link.kind": "decision",
            "codeautopsy.decision.id": record.decision_id,
        },
    )


def autopsy_exception(
    exc: BaseException,
    *,
    commit_sha: str,
    file_path: str,
    line: int,
    blast_radius: int = 1,
    settings: Settings | None = None,
    tracer_provider: TracerProvider | None = None,
    context: dict | None = None,
    repo_root: Path | None = None,
) -> ResolveResponse:
    """Called from the sample app's exception path. Mints the linked autopsy span.

    `tracer_provider` is injectable so tests can pass an in-memory provider instead of
    touching global OTel state (and instead of making a real network export call).

    `context` is the reproduction input (e.g. the request payload) that triggered the
    crash; `repo_root` is where to log it. Both optional — the Fix Bot reads this incident
    log later to synthesize a regression test, but a missing incident never blocks the
    autopsy span itself.
    """
    settings = settings or get_settings()
    resolution = resolve_decision(settings, commit_sha, file_path, line)

    tracer = trace.get_tracer("codeautopsy.enricher", tracer_provider=tracer_provider)
    links = []
    if resolution.resolved and resolution.record:
        link = _decision_link(resolution.record)
        if link is not None:
            links.append(link)

    span = tracer.start_span("codeautopsy.autopsy", links=links)
    span_ctx = span.get_span_context()
    resolution.crash_trace_id = format(span_ctx.trace_id, "032x")
    resolution.crash_span_id = format(span_ctx.span_id, "016x")

    exc_type = type(exc).__name__
    cause = CAUSE_OF_DEATH_BY_EXC.get(exc_type, f"unhandled {exc_type}: {exc}")

    span.set_attribute("codeautopsy.cause_of_death", cause)
    span.set_attribute("codeautopsy.resolved", resolution.resolved)
    span.set_attribute("codeautopsy.blast_radius", blast_radius)
    span.set_attribute("code.filepath", file_path)
    span.set_attribute("code.lineno", line)
    span.set_attribute("deployment.commit.sha", commit_sha)

    if resolution.resolved and resolution.record:
        rec = resolution.record
        span.set_attribute("codeautopsy.decision.id", rec.decision_id)
        span.set_attribute("codeautopsy.decision.summary", rec.reasoning_summary)
        span.set_attribute("codeautopsy.decision.trace_id", rec.decision_trace_id)
        span.set_attribute("codeautopsy.decision.span_id", rec.decision_span_id)
        span.set_attribute("codeautopsy.risk_flags", ",".join(rec.risk_flags))
        span.set_attribute("codeautopsy.decision.session_id", rec.session_id)
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_attribute(
            "codeautopsy.decision.summary", resolution.detail or "no provenance found"
        )
        span.set_status(Status(StatusCode.ERROR, "autopsy could not resolve a decision"))

    span.end()

    # Cloud Run only guarantees CPU while a request is in flight — the BatchSpanProcessor's
    # background export thread can get frozen before it flushes once this response returns.
    # Force the flush now, inside the request's CPU-active window, so the crash span is
    # actually on the wire before autopsy_exception() hands back to the caller.
    provider = tracer_provider or trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is not None:
        force_flush(timeout_millis=3000)

    if repo_root is not None:
        record_incident(
            repo_root,
            file_path=file_path,
            line=line,
            exc_type=exc_type,
            exc_message=str(exc),
            cause_of_death=cause,
            resolved=resolution.resolved,
            provenance=resolution.record,
            context=context,
            blast_radius=blast_radius,
        )

    return resolution
