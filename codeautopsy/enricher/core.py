"""Autopsy Enricher — the join engine's runtime half.

On a recorded exception, resolves the crashing file:line against the provenance service
and mints a linked `codeautopsy.autopsy` child span carrying an OTel span link back to the
AI decision span that authored the line. This is the exact mechanism validated in the
Day-0 smoke test (see scripts/day0_smoke.py) — here it runs against a *real* exception
instead of a hand-constructed one.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber, get_logger, get_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags
from opentelemetry.util.types import AnyValue

from codeautopsy.config import Settings, get_settings
from codeautopsy.enricher.incidents import record_incident
from codeautopsy.provenance.models import ProvenanceRecord, ResolveRequest, ResolveResponse

CAUSE_OF_DEATH_BY_EXC: dict[str, str] = {
    "ValueError": "invalid value — unvalidated input",
    "TypeError": "type mismatch — an unchecked assumption about input shape",
    "KeyError": "missing key — unvalidated dict access",
    "AttributeError": "attribute access on an unexpected None/type",
    "IndexError": "out-of-range access — unvalidated collection bounds",
    "ZeroDivisionError": "division by zero — unvalidated denominator",
}


def resolve_decision(
    settings: Settings,
    commit_sha: str,
    file_path: str,
    line: int,
    *,
    exc_type: str = "",
    exc_message: str = "",
    blast_radius: int = 1,
) -> ResolveResponse:
    """Ask the provenance service which AI decision authored this crashing line.

    When an org `api_key` is configured, resolve against the authenticated `/v1/resolve`:
    the lookup is scoped to that org's decisions and the crash is persisted as an incident on
    the org's dashboard. Without a key, fall back to the public `/resolve` (demo tenant).
    """
    req = ResolveRequest(
        commit_sha=commit_sha,
        file_path=file_path,
        line=line,
        exc_type=exc_type,
        exc_message=exc_message,
        blast_radius=blast_radius,
        ci_run_url=settings.ci_run_url,
    )
    if settings.api_key:
        url = f"{settings.provenance_url}/v1/resolve"
        headers = {"X-Api-Key": settings.api_key}
    else:
        url = f"{settings.provenance_url}/resolve"
        headers = {}
    try:
        resp = httpx.post(url, json=req.model_dump(), headers=headers, timeout=3.0)
        resp.raise_for_status()
        return ResolveResponse(**resp.json())
    except httpx.HTTPError as exc:
        return ResolveResponse(resolved=False, detail=f"provenance service unreachable: {exc}")


def locate_crash_frame(exc: BaseException, repo_root: Path) -> tuple[str, int]:
    """Find the (repo-relative file, line) that actually did the crashing app-side call.

    The literal last traceback frame is not always the app's own code: if the app calls
    into a library function (e.g. ``datetime.strptime``) and *that* raises, the last frame
    lives inside the library/stdlib — a file that isn't part of the app's repo and can't be
    blamed. Walk backwards from the end of the traceback for the last frame whose file is
    actually under ``repo_root``; only fall back to the literal last frame (by bare
    filename) if no frame in the whole traceback is inside the repo at all.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(frames):
        try:
            rel = Path(frame.filename).resolve().relative_to(repo_root.resolve())
        except (ValueError, OSError):
            continue
        return str(rel).replace("\\", "/"), frame.lineno or 0

    last = frames[-1] if frames else None
    if last is None:
        return "unknown", 0
    return Path(last.filename).name, last.lineno or 0


def _decision_link(record: ProvenanceRecord) -> Link | None:
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


def _emit_autopsy_log(
    span_ctx: SpanContext,
    resolution: ResolveResponse,
    cause: str,
    *,
    file_path: str,
    line: int,
    commit_sha: str,
    logger_provider: LoggerProvider | None = None,
) -> None:
    """Emit a trace-correlated log record carrying the crash's cause of death.

    When resolved, the log body is the AI's own reasoning — so the *why* behind a crash is
    queryable as a SigNoz log line (filter/search over `codeautopsy.decision.summary`), not
    just a span attribute you have to open a trace to see. `trace_id`/`span_id` are set
    explicitly from the autopsy span's own context so this correlates to that exact span
    regardless of the ambient OTel context the caller happens to be in.
    """
    logger = get_logger("codeautopsy.enricher", logger_provider=logger_provider)
    if resolution.resolved and resolution.record:
        rec = resolution.record
        severity = SeverityNumber.INFO
        body = f"autopsy resolved: {cause} — {rec.reasoning_summary}"
        attributes: dict[str, AnyValue] = {
            "codeautopsy.decision.id": rec.decision_id,
            "codeautopsy.decision.summary": rec.reasoning_summary,
            "codeautopsy.risk_flags": ",".join(rec.risk_flags),
        }
    else:
        severity = SeverityNumber.WARN
        body = f"autopsy unresolved: {cause}"
        attributes = {}
    attributes.update(
        {
            "code.filepath": file_path,
            "code.lineno": line,
            "deployment.commit.sha": commit_sha,
            "codeautopsy.resolved": resolution.resolved,
        }
    )
    logger.emit(
        LogRecord(
            trace_id=span_ctx.trace_id,
            span_id=span_ctx.span_id,
            trace_flags=span_ctx.trace_flags,
            severity_number=severity,
            severity_text=severity.name,
            body=body,
            attributes=attributes,
        )
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
    logger_provider: LoggerProvider | None = None,
    context: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> ResolveResponse:
    """Called from the sample app's exception path. Mints the linked autopsy span.

    `tracer_provider` / `logger_provider` are injectable so tests can pass in-memory
    providers instead of touching global OTel state (and instead of making a real network
    export call).

    `context` is the reproduction input (e.g. the request payload) that triggered the
    crash; `repo_root` is where to log it. Both optional — the Fix Bot reads this incident
    log later to synthesize a regression test, but a missing incident never blocks the
    autopsy span itself.
    """
    settings = settings or get_settings()
    exc_type = type(exc).__name__
    resolution = resolve_decision(
        settings,
        commit_sha,
        file_path,
        line,
        exc_type=exc_type,
        exc_message=str(exc),
        blast_radius=blast_radius,
    )

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

    cause = CAUSE_OF_DEATH_BY_EXC.get(exc_type, f"unhandled {exc_type}: {exc}")

    span.set_attribute("codeautopsy.cause_of_death", cause)
    span.set_attribute("codeautopsy.resolved", resolution.resolved)
    span.set_attribute("codeautopsy.blast_radius", blast_radius)
    span.set_attribute("code.filepath", file_path)
    span.set_attribute("code.lineno", line)
    span.set_attribute("deployment.commit.sha", commit_sha)
    if settings.ci_run_url:
        # Extends the chain of custody one hop past the decision: reasoning -> commit -> this
        # CI run -> the deployed revision that actually crashed.
        span.set_attribute("deployment.ci_run_url", settings.ci_run_url)

    if resolution.resolved and resolution.record:
        rec = resolution.record
        span.set_attribute("codeautopsy.decision.id", rec.decision_id)
        span.set_attribute("codeautopsy.decision.summary", rec.reasoning_summary)
        span.set_attribute("codeautopsy.decision.trace_id", rec.decision_trace_id)
        span.set_attribute("codeautopsy.decision.span_id", rec.decision_span_id)
        span.set_attribute("codeautopsy.risk_flags", ",".join(rec.risk_flags))
        span.set_attribute("codeautopsy.decision.session_id", rec.session_id)
        if resolution.confidence is not None:
            span.set_attribute("codeautopsy.attribution.confidence", resolution.confidence)
        if resolution.confidence_factors:
            span.set_attribute(
                "codeautopsy.attribution.label", resolution.confidence_factors["label"]
            )
            span.set_attribute(
                "codeautopsy.attribution.match", resolution.confidence_factors["match"]
            )
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_attribute(
            "codeautopsy.decision.summary", resolution.detail or "no provenance found"
        )
        span.set_status(Status(StatusCode.ERROR, "autopsy could not resolve a decision"))

    span.end()
    _emit_autopsy_log(
        span_ctx,
        resolution,
        cause,
        file_path=file_path,
        line=line,
        commit_sha=commit_sha,
        logger_provider=logger_provider,
    )

    # Cloud Run only guarantees CPU while a request is in flight — the BatchSpanProcessor's
    # (and BatchLogRecordProcessor's) background export thread can get frozen before it
    # flushes once this response returns. Force both flushes now, inside the request's
    # CPU-active window, so the crash span and its reasoning log are actually on the wire
    # before autopsy_exception() hands back to the caller.
    provider = tracer_provider or trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is not None:
        force_flush(timeout_millis=3000)

    log_provider = logger_provider or get_logger_provider()
    log_force_flush = getattr(log_provider, "force_flush", None)
    if log_force_flush is not None:
        log_force_flush(timeout_millis=3000)

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
