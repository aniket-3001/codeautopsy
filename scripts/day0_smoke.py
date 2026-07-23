"""Day-0 de-risk: prove the span-link click across the build/run boundary.

This is the single most important experiment in CodeAutopsy. It emits TWO separate
traces and joins them with an OTel *span link*:

    DEV-TIME trace   :  agent.turn -> agent.tool.Edit   (the AI writes buggy code)
    RUNTIME trace    :  POST /checkout -> parse_discount(exception)
                        -> codeautopsy.autopsy  ── span link ──►  the Edit span

If SigNoz renders that link as a clickable "Reference" and lets you jump from the
runtime crash back to the dev-time decision, the entire product thesis is validated.
If not, we pivot to attribute deep-links *before* building anything else.

Run:
    python scripts/day0_smoke.py
    # point elsewhere with:  OTEL_EXPORTER_OTLP_ENDPOINT=http://host:4318 python scripts/day0_smoke.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252 and choke on unicode; force UTF-8 output.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode, TraceFlags


def _load_dotenv() -> None:
    """Tiny zero-dependency .env loader so secrets stay out of chat/history."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

OTLP_BASE = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
TRACES_ENDPOINT = f"{OTLP_BASE}/v1/traces"

# SigNoz Cloud auth: an ingestion key sent as the `signoz-ingestion-key` header.
_headers: dict[str, str] = {}
if key := os.getenv("SIGNOZ_INGESTION_KEY"):
    _headers["signoz-ingestion-key"] = key


def _provider(service_name: str) -> TracerProvider:
    """A dedicated provider per service so each trace shows the right service.name."""
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
                "deployment.environment": "codeautopsy-day0",
            }
        )
    )
    exporter = OTLPSpanExporter(endpoint=TRACES_ENDPOINT, headers=_headers or None)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def hex_id(value: int, width: int) -> str:
    return format(value, f"0{width}x")


def main() -> None:
    print(f"→ exporting to {TRACES_ENDPOINT}\n")

    # ---- DEV-TIME: the AI agent writes the buggy line -----------------------------
    dev_provider = _provider("claude-code")
    dev_tracer = dev_provider.get_tracer("codeautopsy.recorder")

    decision_ctx: SpanContext | None = None
    with dev_tracer.start_as_current_span("agent.turn") as turn:
        turn.set_attribute("agent.session.id", "sess_day0")
        turn.set_attribute("agent.model", "claude-opus-4-8")
        turn.set_attribute("user.prompt", "add a discount-code endpoint")

        with dev_tracer.start_as_current_span("agent.tool.Edit") as edit:
            edit.set_attribute("agent.tool.name", "Edit")
            edit.set_attribute("code.file.path", "app/payment.py")
            edit.set_attribute("code.lines.start", 40)
            edit.set_attribute("code.lines.end", 45)
            edit.set_attribute("agent.reasoning", "assuming the input is always valid, parse directly")
            edit.set_attribute("agent.decision.id", "dec_7f3a")
            edit.set_attribute("vcs.commit.sha", "abc123")
            edit.set_attribute("codeautopsy.risk_flags", "assumed_valid_input,skipped_tests")
            # Capture the decision span's context — this is what the runtime trace links to.
            decision_ctx = edit.get_span_context()

    dev_provider.force_flush()

    assert decision_ctx is not None
    print("DEV-TIME decision span (the target of the jump):")
    print(f"  trace_id = {hex_id(decision_ctx.trace_id, 32)}")
    print(f"  span_id  = {hex_id(decision_ctx.span_id, 16)}\n")

    # A clean, serializable link target (survives across provider boundaries).
    link_target = SpanContext(
        trace_id=decision_ctx.trace_id,
        span_id=decision_ctx.span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )

    # ---- RUNTIME: the crash, then the autopsy span carrying the link --------------
    run_provider = _provider("checkout-api")
    run_tracer = run_provider.get_tracer("codeautopsy.enricher")

    with run_tracer.start_as_current_span("POST /checkout", kind=SpanKind.SERVER) as request:
        request.set_attribute("http.method", "POST")
        request.set_attribute("http.route", "/checkout")
        request.set_attribute("deployment.commit.sha", "abc123")

        with run_tracer.start_as_current_span("parse_discount") as crash:
            try:
                raise ValueError("invalid literal for int() with base 10: 'GIMME50'")
            except ValueError as exc:
                crash.record_exception(exc)
                crash.set_status(Status(StatusCode.ERROR, "unvalidated int() on user input"))
                crash.set_attribute("code.filepath", "app/payment.py")
                crash.set_attribute("code.lineno", 42)
                crash.set_attribute("deployment.commit.sha", "abc123")

            # THE JUMP: an autopsy span whose *link* points back to the AI's decision.
            autopsy = run_tracer.start_span(
                "codeautopsy.autopsy",
                links=[
                    Link(
                        link_target,
                        attributes={
                            "codeautopsy.link.kind": "decision",
                            "codeautopsy.decision.id": "dec_7f3a",
                        },
                    )
                ],
            )
            autopsy.set_attribute("codeautopsy.cause_of_death", "unvalidated int() on user input")
            autopsy.set_attribute("codeautopsy.decision.summary", "assuming the input is always valid")
            autopsy.set_attribute("codeautopsy.decision.id", "dec_7f3a")
            autopsy.set_attribute("codeautopsy.decision.trace_id", hex_id(decision_ctx.trace_id, 32))
            autopsy.set_attribute("codeautopsy.decision.span_id", hex_id(decision_ctx.span_id, 16))
            autopsy.set_attribute("codeautopsy.risk_flags", "assumed_valid_input")
            autopsy.set_attribute("codeautopsy.blast_radius", 137)
            autopsy.end()
            run_request_ctx = request.get_span_context()

    run_provider.force_flush()

    print("RUNTIME crash trace (open THIS one in SigNoz, find codeautopsy.autopsy):")
    print(f"  trace_id = {hex_id(run_request_ctx.trace_id, 32)}\n")

    # Give the batch exporters a moment before the process exits.
    time.sleep(1)
    print("✓ both traces flushed. In SigNoz:")
    print("  1. Open the RUNTIME trace above (service checkout-api).")
    print("  2. Select the codeautopsy.autopsy span.")
    print("  3. Look for a 'References' / linked-span reference and CLICK it.")
    print("  4. If it navigates to the claude-code dev trace → THESIS VALIDATED.")


if __name__ == "__main__":
    main()
