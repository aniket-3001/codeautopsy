"""Tests for the postmortem case-file generator — pure rendering, no git/HTTP/LLM."""

from __future__ import annotations

from codeautopsy.fixbot.models import Genealogy
from codeautopsy.postmortem.core import render_postmortem
from codeautopsy.provenance.models import LessonRecord


def _resolved_genealogy(**overrides) -> Genealogy:
    base = dict(
        file_path="app/checkout.py",
        line=42,
        commit_sha="abc123def456789",
        file_content="def parse_discount(code):\n    return int(code)\n",
        reasoning_summary="assuming discount_code is always a clean integer string",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_7f3a",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        decision_span_id="e91ca75cd1ae81e4",
        exc_type="ValueError",
        exc_message="invalid literal for int() with base 10: 'SAVE20'",
        cause_of_death="invalid value — unvalidated input",
        confidence=1.0,
        confidence_factors={"label": "high", "match": "exact-commit"},
    )
    base.update(overrides)
    return Genealogy(**base)


def test_render_postmortem_resolved_with_lesson():
    genealogy = _resolved_genealogy()
    lesson = LessonRecord(
        fingerprint="abc123",
        lesson="Always validate discount_code before int() conversion.",
        patch_summary="Wrapped int(code) in a try/except, defaulting to 0.",
        times_seen=3,
    )

    md = render_postmortem(genealogy, lesson=lesson, ci_run_url="https://github.com/x/y/actions/runs/1")

    assert "# Postmortem — `app/checkout.py:42`" in md
    assert "abc123def456" in md  # truncated commit sha
    assert "ValueError: invalid literal for int() with base 10: 'SAVE20'" in md
    assert "invalid value — unvalidated input" in md
    assert "assuming discount_code is always a clean integer string" in md
    assert "dec_7f3a" in md
    assert "assumed_valid_input" in md
    assert "100% (high, via exact-commit)" in md
    assert "Always validate discount_code before int() conversion." in md
    assert "Seen **3x**" in md
    assert "[CI run](https://github.com/x/y/actions/runs/1)" in md
    assert "no local incident log entry" not in md
    assert "No lesson recorded" not in md


def test_render_postmortem_resolved_without_lesson():
    genealogy = _resolved_genealogy()

    md = render_postmortem(genealogy)

    assert "No lesson recorded yet for this class of bug" in md
    assert "run `codeautopsy fix` to learn one" in md
    assert "CI run" not in md  # no ci_run_url supplied


def test_render_postmortem_unresolved_degrades_gracefully():
    genealogy = Genealogy(
        file_path="app/other.py",
        line=9,
        commit_sha="deadbeef",
        file_content="x = 1\n",
    )

    md = render_postmortem(genealogy)

    assert "No decision indexed for this line" in md
    assert "no local incident log entry for this coordinate" in md
    assert "**Decision:**" not in md  # only appears in the resolved reasoning section
    assert "4. **Decision** — none found" in md
    assert "No lesson recorded yet" in md


def test_render_postmortem_confidence_omitted_when_none():
    genealogy = _resolved_genealogy(confidence=None, confidence_factors=None)

    md = render_postmortem(genealogy)

    assert "Attribution confidence" not in md
