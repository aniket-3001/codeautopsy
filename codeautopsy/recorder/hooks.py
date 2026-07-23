"""Claude Code hook entrypoints.

`post_tool_use_main()` is invoked BY Claude Code itself as a subprocess hook (PostToolUse,
matcher: Edit|Write). It reads the hook JSON payload from stdin, emits a dev-time OTel
span, and queues a pending provenance decision.

It MUST exit 0 no matter what — a crashing hook breaks the user's live session. All
exception handling lives in the `_main` wrapper; `record_post_tool_use` itself is the pure
testable core and is allowed to raise (so tests catch real bugs).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback as tb_module
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider

from codeautopsy.otel import build_tracer_provider, force_utf8_stdout
from codeautopsy.recorder.pending import append_pending
from codeautopsy.recorder.risk import detect_risk_flags, extract_last_assistant_reasoning

TRACKED_TOOLS = {"Edit", "Write"}


def _decision_id(file_path: str, line_start: int) -> str:
    raw = f"{file_path}:{line_start}:{time.time_ns()}"
    return "dec_" + hashlib.sha256(raw.encode()).hexdigest()[:10]


def _line_range_for_edit(file_path: Path, needle: str) -> tuple[int, int] | None:
    """Locate `new_string` in the (already-edited) file to recover its line range."""
    if not file_path.exists() or not needle:
        return None
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    idx = content.find(needle)
    if idx == -1:
        return None
    start = content.count("\n", 0, idx) + 1
    end = start + needle.count("\n")
    return start, end


def _line_range_for_write(file_path: Path) -> tuple[int, int] | None:
    """A Write authors the whole file — the range is the entire file."""
    if not file_path.exists():
        return None
    try:
        n = len(file_path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None
    return (1, max(n, 1))


def _relative_path(file_path: Path, repo_root: Path) -> str:
    try:
        return str(file_path.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return file_path.name


def record_post_tool_use(
    payload: dict, tracer_provider: TracerProvider | None = None
) -> dict | None:
    """Core logic for the PostToolUse hook. Returns the queued pending-decision dict, or
    None if the tool call wasn't one we track (or produced no usable line range).

    `tracer_provider` is injectable: production builds a fresh per-process provider (each
    hook invocation is its own OS process), tests pass an in-memory provider so no network
    call happens and span contents can be asserted directly.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name not in TRACKED_TOOLS:
        return None

    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "") or "."
    repo_root = Path(cwd)

    file_path_str = tool_input.get("file_path", "")
    if not file_path_str:
        return None
    file_path = Path(file_path_str)

    if tool_name == "Edit":
        code_sample = tool_input.get("new_string", "")
        line_range = _line_range_for_edit(file_path, code_sample)
    else:  # Write
        code_sample = tool_input.get("content", "")
        line_range = _line_range_for_write(file_path)

    if line_range is None:
        return None
    line_start, line_end = line_range

    reasoning = extract_last_assistant_reasoning(transcript_path) if transcript_path else ""
    risk_flags = detect_risk_flags(reasoning, code_sample)
    rel_path = _relative_path(file_path, repo_root)
    decision_id = _decision_id(rel_path, line_start)

    provider = tracer_provider or build_tracer_provider("claude-code")
    tracer = provider.get_tracer("codeautopsy.recorder")
    span = tracer.start_span(f"agent.tool.{tool_name}")
    span.set_attribute("agent.tool.name", tool_name)
    span.set_attribute("agent.session.id", session_id)
    span.set_attribute("code.file.path", rel_path)
    span.set_attribute("code.lines.start", line_start)
    span.set_attribute("code.lines.end", line_end)
    span.set_attribute("agent.reasoning", reasoning or "(no reasoning captured)")
    span.set_attribute("agent.decision.id", decision_id)
    span.set_attribute("codeautopsy.risk_flags", ",".join(risk_flags))
    ctx = span.get_span_context()
    span.end()
    if tracer_provider is None:
        provider.force_flush(timeout_millis=3000)
        provider.shutdown()

    record = {
        "file_path": rel_path,
        "line_start": line_start,
        "line_end": line_end,
        "decision_span_id": format(ctx.span_id, "016x"),
        "decision_trace_id": format(ctx.trace_id, "032x"),
        "session_id": session_id,
        "reasoning_summary": reasoning,
        "risk_flags": risk_flags,
        "decision_id": decision_id,
        "tool": "claude-code",
    }
    append_pending(repo_root, record)
    return record


def _log_hook_error(cwd: str) -> None:
    """Best-effort error logging that itself must never raise."""
    try:
        d = Path(cwd or ".") / ".codeautopsy"
        d.mkdir(exist_ok=True)
        with (d / "hook_errors.log").open("a", encoding="utf-8") as f:
            f.write(tb_module.format_exc() + "\n")
    except Exception:  # noqa: BLE001
        pass


def post_tool_use_main() -> int:
    """The real Claude Code hook entrypoint. Reads stdin, never raises, always exits 0."""
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0

    try:
        record_post_tool_use(payload)
    except Exception:  # noqa: BLE001
        _log_hook_error(payload.get("cwd", ""))
    return 0


def main() -> None:
    force_utf8_stdout()
    event = sys.argv[1] if len(sys.argv) > 1 else "post-tool-use"
    if event == "post-tool-use":
        sys.exit(post_tool_use_main())
    sys.exit(0)


if __name__ == "__main__":
    main()
