"""CodeAutopsy sample app — the 'patient'.

A small checkout API with a seeded bug (`parse_discount` below), fully instrumented so a
real crash produces a real, resolvable autopsy: crash -> git-blame join -> AI decision.
"""

from __future__ import annotations

import collections
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode

from codeautopsy.config import get_settings
from codeautopsy.enricher.core import autopsy_exception, locate_crash_frame
from codeautopsy.otel import build_meter_provider, build_tracer_provider, force_utf8_stdout

force_utf8_stdout()

# codeautopsy/sample_app/main.py -> codeautopsy/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _deployed_commit_sha() -> str:
    """The pinned deploy SHA. Never blame against a moving HEAD in prod — this is set once
    at process start (from CODEAUTOPSY_COMMIT_SHA in containers, or `git rev-parse HEAD`
    locally) so every autopsy in this process's lifetime blames against the same commit
    that's actually running.
    """
    env_sha = os.getenv("CODEAUTOPSY_COMMIT_SHA")
    if env_sha:
        return env_sha
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


DEPLOYED_COMMIT_SHA = _deployed_commit_sha()

settings = get_settings()
_provider = build_tracer_provider(
    settings.runtime_service_name,
    resource_attrs={"deployment.commit.sha": DEPLOYED_COMMIT_SHA},
    settings=settings,
)
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer("codeautopsy.sample_app")

_meter_provider = build_meter_provider(
    settings.runtime_service_name,
    resource_attrs={"deployment.commit.sha": DEPLOYED_COMMIT_SHA},
    settings=settings,
)
metrics.set_meter_provider(_meter_provider)
meter = metrics.get_meter("codeautopsy.sample_app")

# The metric that closes the Auto-Heal loop: a real OTel Counter SigNoz can alert on. When
# a judge crashes this endpoint, this counter's rate spikes, a SigNoz alert rule fires its
# webhook, and the heal loop kicks off.
crash_counter = meter.create_counter(
    "codeautopsy.crashes",
    unit="1",
    description="Unhandled crashes in the sample checkout app, by crashing file:line.",
)

app = FastAPI(title="CodeAutopsy Sample App — checkout-api")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://aniket-3001.github.io",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
FastAPIInstrumentor.instrument_app(app)

# Crude in-process blast-radius counter: how many times has THIS line crashed this run?
_crash_counts: collections.Counter = collections.Counter()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "commit": DEPLOYED_COMMIT_SHA}


def parse_discount(code: str) -> int:
    """Parse a numeric discount code.

    THE SEEDED BUG: assumes `code` is always a clean integer string and never validates
    it — exactly the kind of shortcut an AI coding agent takes under time pressure.
    CodeAutopsy exists to point at this exact line and the reasoning that produced it.
    """
    return int(code)  # <-- the crashing line codeautopsy/sample_app/main.py


@app.post("/checkout")
def checkout(payload: dict) -> dict:
    with tracer.start_as_current_span("parse_discount") as span:
        try:
            discount = parse_discount(payload.get("discount_code", ""))
        except Exception as exc:  # noqa: BLE001 — deliberately broad: this is the crash we autopsy
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))

            rel_path, lineno = locate_crash_frame(exc, REPO_ROOT)

            key = (rel_path, lineno)
            _crash_counts[key] += 1
            crash_counter.add(
                1,
                {
                    "service.name": settings.runtime_service_name,
                    "file": rel_path,
                    "line": lineno,
                    "commit.sha": DEPLOYED_COMMIT_SHA,
                },
            )

            resolution = autopsy_exception(
                exc,
                commit_sha=DEPLOYED_COMMIT_SHA,
                file_path=rel_path,
                line=lineno,
                blast_radius=_crash_counts[key],
                settings=settings,
                context=payload,
                repo_root=REPO_ROOT,
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "error": str(exc),
                    "codeautopsy": {
                        "resolved": resolution.resolved,
                        "decision_summary": (
                            resolution.record.reasoning_summary if resolution.record else None
                        ),
                        "risk_flags": resolution.record.risk_flags if resolution.record else [],
                        "crash_trace_id": resolution.crash_trace_id,
                        "crash_span_id": resolution.crash_span_id,
                    },
                },
            ) from exc

    total = payload.get("subtotal", 100) - discount
    return {"total": total, "discount_applied": discount}


def run() -> None:
    """Entry point: `codeautopsy-sample`."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
