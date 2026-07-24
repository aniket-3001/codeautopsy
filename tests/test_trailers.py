"""Tests for AI-provenance git trailers (`codeautopsy.provenance.trailers`).

The pure format/parse pair is exercised directly; `read_commit_trailers` is exercised against a
throwaway real git repo so the round-trip through an actual commit footer is covered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeautopsy.provenance.trailers import (
    TRAILER_COORDINATE,
    TRAILER_DECISION_ID,
    TRAILER_TRACEPARENT,
    build_traceparent,
    format_trailers,
    parse_trailers,
    read_commit_trailers,
)

_TRACE = "7b5b1b39741a991f073d59e245fb7575"
_SPAN = "00f067aa0ba902b7"


def test_build_traceparent_valid() -> None:
    assert build_traceparent(_TRACE, _SPAN) == f"00-{_TRACE}-{_SPAN}-01"


def test_build_traceparent_uppercases_to_lower() -> None:
    assert build_traceparent(_TRACE.upper(), _SPAN.upper()) == f"00-{_TRACE}-{_SPAN}-01"


@pytest.mark.parametrize(
    "trace,span",
    [("", _SPAN), (_TRACE, ""), ("xyz", _SPAN), (_TRACE, "short"), (None, None)],
)
def test_build_traceparent_rejects_malformed(trace, span) -> None:
    assert build_traceparent(trace, span) is None


def test_format_trailers_full_block() -> None:
    block = format_trailers(
        decision_id="dec_abc", trace_id=_TRACE, span_id=_SPAN, coordinate="app/pay.py:42@abc123"
    )
    assert f"{TRAILER_DECISION_ID}: dec_abc" in block
    assert f"{TRAILER_TRACEPARENT}: 00-{_TRACE}-{_SPAN}-01" in block
    assert f"{TRAILER_COORDINATE}: app/pay.py:42@abc123" in block


def test_format_trailers_empty_when_nothing_to_stamp() -> None:
    assert format_trailers() == ""
    # A bad traceparent alone must not fabricate a line.
    assert format_trailers(trace_id="bad", span_id="also-bad") == ""


def test_parse_roundtrips_format() -> None:
    block = format_trailers(
        decision_id="dec_abc", trace_id=_TRACE, span_id=_SPAN, coordinate="app/pay.py:42@abc123"
    )
    message = f"Fix Bot: patch\n\nsome body text\n\n{block}"
    parsed = parse_trailers(message)
    assert parsed["decision_id"] == "dec_abc"
    assert parsed["coordinate"] == "app/pay.py:42@abc123"
    assert parsed["trace_id"] == _TRACE
    assert parsed["span_id"] == _SPAN


def test_parse_empty_when_no_trailers() -> None:
    assert parse_trailers("just a normal commit\n\nwith a body") == {}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_read_commit_trailers_from_real_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.dev")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "f.py")
    block = format_trailers(decision_id="dec_xyz", trace_id=_TRACE, span_id=_SPAN)
    _git(tmp_path, "commit", "-q", "-m", f"patch it\n\nbody\n\n{block}")

    found = read_commit_trailers(tmp_path, "HEAD")
    assert found["decision_id"] == "dec_xyz"
    assert found["trace_id"] == _TRACE


def test_read_commit_trailers_bad_ref_is_quiet(tmp_path: Path) -> None:
    # Not even a git repo -> quiet empty dict, never an exception.
    assert read_commit_trailers(tmp_path, "deadbeef") == {}
