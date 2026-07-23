"""Tests for the Autopsy Enricher — the exact span-link mechanism validated live in SigNoz.

Uses an in-memory span exporter (no network, no global OTel state) so these tests are a
fast, deterministic regression guard on the core mechanism: crash -> resolve -> linked span.
"""

from __future__ import annotations

from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from codeautopsy.enricher.core import CAUSE_OF_DEATH_BY_EXC, autopsy_exception
from codeautopsy.enricher.incidents import latest_incident_for, read_incidents
from codeautopsy.provenance.models import ProvenanceRecord, ResolveResponse


def _memory_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _record(**overrides) -> ProvenanceRecord:
    base = dict(
        commit_sha="abc123",
        file_path="app.py",
        line_start=2,
        line_end=2,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="s1",
        reasoning_summary="assuming input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_1",
    )
    base.update(overrides)
    return ProvenanceRecord(**base)


def test_autopsy_exception_resolved_creates_span_link(monkeypatch):
    rec = _record()
    monkeypatch.setattr(
        "codeautopsy.enricher.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=True, introducing_commit="abc123", record=rec),
    )
    provider, exporter = _memory_provider()

    try:
        int("GIMME50")
    except ValueError as exc:
        resolution = autopsy_exception(
            exc, commit_sha="abc123", file_path="app.py", line=2, tracer_provider=provider
        )

    assert resolution.resolved is True

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    autopsy_span = spans[0]
    assert autopsy_span.name == "codeautopsy.autopsy"

    # THE mechanism: the autopsy span must carry a link pointing at the decision span.
    assert len(autopsy_span.links) == 1
    link = autopsy_span.links[0]
    assert link.context.span_id == int(rec.decision_span_id, 16)
    assert link.context.trace_id == int(rec.decision_trace_id, 16)
    assert link.attributes["codeautopsy.decision.id"] == "dec_1"

    assert autopsy_span.attributes["codeautopsy.cause_of_death"] == CAUSE_OF_DEATH_BY_EXC["ValueError"]
    assert autopsy_span.attributes["codeautopsy.decision.summary"] == "assuming input is always valid"
    assert autopsy_span.attributes["codeautopsy.risk_flags"] == "assumed_valid_input"


def test_autopsy_exception_unresolved_has_no_link(monkeypatch):
    monkeypatch.setattr(
        "codeautopsy.enricher.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=False, detail="no provenance found"),
    )
    provider, exporter = _memory_provider()

    try:
        raise KeyError("missing")
    except KeyError as exc:
        resolution = autopsy_exception(
            exc, commit_sha="x", file_path="f.py", line=1, tracer_provider=provider
        )

    assert resolution.resolved is False
    span = exporter.get_finished_spans()[0]
    assert span.links == ()
    assert span.attributes["codeautopsy.cause_of_death"] == CAUSE_OF_DEATH_BY_EXC["KeyError"]


def test_autopsy_exception_malformed_ids_dont_crash(monkeypatch):
    """A corrupted provenance record (bad hex ids) must degrade gracefully, never raise."""
    bad_rec = _record(decision_trace_id="not-hex", decision_span_id="also-not-hex")
    monkeypatch.setattr(
        "codeautopsy.enricher.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=True, record=bad_rec),
    )
    provider, exporter = _memory_provider()

    try:
        raise TypeError("boom")
    except TypeError as exc:
        autopsy_exception(exc, commit_sha="x", file_path="f.py", line=1, tracer_provider=provider)

    span = exporter.get_finished_spans()[0]
    assert span.links == ()  # link creation failed safely, no crash


def test_unknown_exception_type_gets_generic_cause(monkeypatch):
    monkeypatch.setattr(
        "codeautopsy.enricher.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=False, detail="n/a"),
    )
    provider, exporter = _memory_provider()

    class WeirdError(Exception):
        pass

    try:
        raise WeirdError("something odd")
    except WeirdError as exc:
        autopsy_exception(exc, commit_sha="x", file_path="f.py", line=1, tracer_provider=provider)

    span = exporter.get_finished_spans()[0]
    assert "WeirdError" in span.attributes["codeautopsy.cause_of_death"]


# --- incident logging (what the Fix Bot reads later) ---------------------------------------


def test_autopsy_exception_logs_incident_when_repo_root_given(tmp_path: Path, monkeypatch):
    rec = _record()
    monkeypatch.setattr(
        "codeautopsy.enricher.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=True, introducing_commit="abc123", record=rec),
    )
    provider, _ = _memory_provider()

    try:
        int("GIMME50")
    except ValueError as exc:
        autopsy_exception(
            exc,
            commit_sha="abc123",
            file_path="app.py",
            line=2,
            tracer_provider=provider,
            context={"discount_code": "GIMME50"},
            repo_root=tmp_path,
        )

    incidents = read_incidents(tmp_path)
    assert len(incidents) == 1
    assert incidents[0]["exc_type"] == "ValueError"
    assert incidents[0]["decision_id"] == "dec_1"
    assert incidents[0]["risk_flags"] == ["assumed_valid_input"]
    assert incidents[0]["context"] == {"discount_code": "GIMME50"}

    latest = latest_incident_for(tmp_path, "app.py", 2)
    assert latest is not None
    assert latest["decision_id"] == "dec_1"


def test_autopsy_exception_no_incident_without_repo_root(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "codeautopsy.enricher.core.resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=False, detail="n/a"),
    )
    provider, _ = _memory_provider()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        autopsy_exception(exc, commit_sha="x", file_path="f.py", line=1, tracer_provider=provider)

    assert read_incidents(tmp_path) == []
