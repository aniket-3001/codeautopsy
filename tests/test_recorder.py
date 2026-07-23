"""Tests for the Recorder: risk-flag detection, pending queue, hook core logic, and the
commit indexer that binds pending decisions to a real commit via git blame.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from codeautopsy.provenance.store import ProvenanceStore
from codeautopsy.recorder.commit_indexer import index_pending_at_head
from codeautopsy.recorder.hooks import record_post_tool_use
from codeautopsy.recorder.pending import append_pending, clear_pending, read_pending
from codeautopsy.recorder.risk import detect_risk_flags, extract_last_assistant_reasoning


def _memory_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


# --- risk flags ---------------------------------------------------------------------------


def test_detect_risk_flags_assumed_valid_input():
    flags = detect_risk_flags("assuming the input is always valid, parse directly", "int(code)")
    assert "assumed_valid_input" in flags


def test_detect_risk_flags_multiple():
    flags = detect_risk_flags("I think this should work, skipping tests for now", "return x")
    assert "uncertainty" in flags
    assert "skipped_tests" in flags


def test_detect_risk_flags_empty():
    assert detect_risk_flags("", "") == []
    assert detect_risk_flags() == []


def test_detect_risk_flags_clean_code():
    assert detect_risk_flags("added input validation with a regex", "if not code.isdigit(): raise") == []


def test_extract_reasoning_missing_transcript(tmp_path: Path):
    assert extract_last_assistant_reasoning(tmp_path / "nope.jsonl") == ""


def test_extract_reasoning_from_transcript(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type": "user", "message": {"content": "add discount parsing"}}\n'
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "assuming input is always valid"}]}}\n',
        encoding="utf-8",
    )
    assert extract_last_assistant_reasoning(transcript) == "assuming input is always valid"


def test_extract_reasoning_malformed_lines_dont_crash(tmp_path: Path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("not json\n{broken\n", encoding="utf-8")
    assert extract_last_assistant_reasoning(transcript) == ""


# --- pending queue -------------------------------------------------------------------------


def test_pending_queue_roundtrip(tmp_path: Path):
    assert read_pending(tmp_path) == []
    append_pending(tmp_path, {"decision_id": "dec_1"})
    append_pending(tmp_path, {"decision_id": "dec_2"})
    pending = read_pending(tmp_path)
    assert [p["decision_id"] for p in pending] == ["dec_1", "dec_2"]
    clear_pending(tmp_path)
    assert read_pending(tmp_path) == []


# --- hook core logic -----------------------------------------------------------------------


def test_record_post_tool_use_edit(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("def f():\n    return int(code)\n", encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "session_id": "sess_1",
        "cwd": str(tmp_path),
        "transcript_path": "",
        "tool_input": {
            "file_path": str(target),
            "old_string": "pass",
            "new_string": "return int(code)",
        },
    }
    provider, exporter = _memory_provider()
    record = record_post_tool_use(payload, tracer_provider=provider)

    assert record is not None
    assert record["file_path"] == "app.py"
    assert record["line_start"] == 2
    assert record["line_end"] == 2

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.tool.Edit"
    assert spans[0].attributes["code.file.path"] == "app.py"
    assert spans[0].attributes["code.lines.start"] == 2

    pending = read_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["decision_id"] == record["decision_id"]


def test_record_post_tool_use_write_whole_file(tmp_path: Path):
    target = tmp_path / "new_module.py"
    content = "import os\n\ndef f():\n    return 1\n"
    target.write_text(content, encoding="utf-8")

    payload = {
        "tool_name": "Write",
        "session_id": "sess_2",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(target), "content": content},
    }
    provider, exporter = _memory_provider()
    record = record_post_tool_use(payload, tracer_provider=provider)

    assert record is not None
    assert record["line_start"] == 1
    assert record["line_end"] == 4
    assert exporter.get_finished_spans()[0].name == "agent.tool.Write"


def test_record_post_tool_use_ignores_untracked_tools(tmp_path: Path):
    provider, exporter = _memory_provider()
    result = record_post_tool_use({"tool_name": "Read", "cwd": str(tmp_path)}, tracer_provider=provider)
    assert result is None
    assert len(exporter.get_finished_spans()) == 0


def test_record_post_tool_use_missing_file_path(tmp_path: Path):
    provider, _ = _memory_provider()
    payload = {"tool_name": "Edit", "cwd": str(tmp_path), "tool_input": {}}
    assert record_post_tool_use(payload, tracer_provider=provider) is None


def test_record_post_tool_use_captures_risk_flags(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("x = int(code)\n", encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"type": "assistant", "message": {"content": [{"type": "text", '
        '"text": "assuming the input is always valid here"}]}}\n',
        encoding="utf-8",
    )
    payload = {
        "tool_name": "Edit",
        "session_id": "sess_3",
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
        "tool_input": {"file_path": str(target), "old_string": "", "new_string": "x = int(code)"},
    }
    provider, _ = _memory_provider()
    record = record_post_tool_use(payload, tracer_provider=provider)
    assert "assumed_valid_input" in record["risk_flags"]
    assert record["reasoning_summary"] == "assuming the input is always valid here"


# --- commit indexer (real git repo, end-to-end) --------------------------------------------


def test_index_pending_at_head_binds_real_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")

    target = repo / "app.py"
    target.write_text("x = int(code)\n", encoding="utf-8")

    # Simulate the recorder queueing a decision BEFORE the commit exists.
    append_pending(
        repo,
        {
            "file_path": "app.py",
            "line_start": 1,
            "line_end": 1,
            "decision_span_id": "e91ca75cd1ae81e4",
            "decision_trace_id": "c51641b768a8a67ea979f9005ade2f55",
            "session_id": "sess_1",
            "reasoning_summary": "assuming input is always valid",
            "risk_flags": ["assumed_valid_input"],
            "decision_id": "dec_1",
            "tool": "claude-code",
        },
    )

    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "add discount parse")
    head = _git(repo, "rev-parse", "HEAD")

    store = ProvenanceStore(tmp_path / "p.db")
    n = index_pending_at_head(repo, store)
    assert n == 1
    assert read_pending(repo) == []  # queue cleared

    rec = store.find_by_line(head, "app.py", 1)
    assert rec is not None
    assert rec.decision_id == "dec_1"
    assert rec.commit_sha == head


def test_index_pending_noop_when_empty(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    store = ProvenanceStore(tmp_path / "p.db")
    assert index_pending_at_head(repo, store) == 0
