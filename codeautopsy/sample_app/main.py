"""CodeAutopsy sample app — the 'patient'.

A small checkout API with a seeded bug (`parse_discount` below), fully instrumented so a
real crash produces a real, resolvable autopsy: crash -> git-blame join -> AI decision.
"""

from __future__ import annotations

import collections
import os
import subprocess
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode

from codeautopsy.config import get_settings
from codeautopsy.enricher.core import autopsy_exception
from codeautopsy.otel import build_tracer_provider, force_utf8_stdout

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

app = FastAPI(title="CodeAutopsy Sample App — checkout-api")
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

            frames = traceback.extract_tb(exc.__traceback__)
            last = frames[-1] if frames else None
            file_path = last.filename if last else __file__
            lineno = (last.lineno or 0) if last else 0
            try:
                rel_path = str(Path(file_path).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
            except ValueError:
                rel_path = Path(file_path).name

            key = (rel_path, lineno)
            _crash_counts[key] += 1

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
