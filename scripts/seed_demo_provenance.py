"""Seed the provenance store with a decision matching the sample app's seeded bug at HEAD,
so a live `POST /checkout` crash resolves end-to-end through git blame + the provenance
service — and the autopsy span's link navigates into a REAL dev-time trace in SigNoz.

This simulates what the Recorder does automatically after `git commit`, without needing to
run the actual Claude Code hooks live — useful for a fast, repeatable demo.

Prerequisites:
    codeautopsy-provenance   # running on :8100 (or set CODEAUTOPSY_PROVENANCE_URL)

Run:
    python scripts/seed_demo_provenance.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codeautopsy.config import get_settings  # noqa: E402
from codeautopsy.otel import build_tracer_provider, force_utf8_stdout  # noqa: E402
from codeautopsy.provenance.models import ProvenanceRecord  # noqa: E402

force_utf8_stdout()

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = REPO_ROOT / "codeautopsy" / "sample_app" / "main.py"
NEEDLE = "return int(code)"


def head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def find_line(path: Path, needle: str) -> int:
    text = path.read_text(encoding="utf-8")
    idx = text.index(needle)
    return text.count("\n", 0, idx) + 1


def main() -> None:
    settings = get_settings()
    sha = head_sha()
    line = find_line(TARGET_FILE, NEEDLE)
    rel_path = str(TARGET_FILE.relative_to(REPO_ROOT)).replace("\\", "/")

    print(f"Seeding provenance for {rel_path}:{line} @ {sha[:8]}\n")

    provider = build_tracer_provider(settings.dev_service_name, settings=settings)
    tracer = provider.get_tracer("codeautopsy.recorder")
    with tracer.start_as_current_span("agent.turn") as turn:
        turn.set_attribute("agent.session.id", "sess_demo")
        turn.set_attribute("user.prompt", "add a discount-code endpoint")
        with tracer.start_as_current_span("agent.tool.Edit") as edit:
            edit.set_attribute("agent.tool.name", "Edit")
            edit.set_attribute("code.file.path", rel_path)
            edit.set_attribute("code.lines.start", line)
            edit.set_attribute("code.lines.end", line)
            edit.set_attribute(
                "agent.reasoning", "assuming the input is always valid, parse directly"
            )
            edit.set_attribute("agent.decision.id", "dec_demo")
            edit.set_attribute("vcs.commit.sha", sha)
            edit.set_attribute("codeautopsy.risk_flags", "assumed_valid_input,skipped_tests")
            ctx = edit.get_span_context()
    provider.force_flush()

    record = ProvenanceRecord(
        commit_sha=sha,
        file_path=rel_path,
        line_start=line,
        line_end=line,
        decision_span_id=format(ctx.span_id, "016x"),
        decision_trace_id=format(ctx.trace_id, "032x"),
        session_id="sess_demo",
        reasoning_summary="assuming the input is always valid, parse directly",
        risk_flags=["assumed_valid_input", "skipped_tests"],
        decision_id="dec_demo",
    )

    resp = httpx.post(f"{settings.provenance_url}/provenance", json=record.model_dump(), timeout=5.0)
    resp.raise_for_status()

    print("Seeded 1 provenance record. Now trigger a REAL crash:\n")
    print('  curl -X POST http://localhost:8000/checkout \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d "{\\"discount_code\\": \\"GIMME50\\"}"')
    print("\nThen open the checkout-api trace in SigNoz, select codeautopsy.autopsy,")
    print("and click the linked span. It should jump into the claude-code dev trace above.")


if __name__ == "__main__":
    main()
