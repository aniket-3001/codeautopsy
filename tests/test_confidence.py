"""Unit tests for attribution confidence (`codeautopsy.provenance.confidence`).

The scorer is pure — it reads a ResolveResponse + the ResolveRequest and returns a 0–1 score
plus an explaining factors dict — so these exercise it directly without a store or a repo.
"""

from __future__ import annotations

from codeautopsy.provenance.confidence import attribution_confidence
from codeautopsy.provenance.models import ProvenanceRecord, ResolveRequest, ResolveResponse


def _record(line_start: int = 40, line_end: int = 46, **over) -> ProvenanceRecord:
    base = dict(
        org_id="demo-public",
        commit_sha="abc123def456",
        file_path="app/payment.py",
        line_start=line_start,
        line_end=line_end,
        decision_span_id="00f067aa0ba902b7",
        decision_trace_id="7b5b1b39741a991f073d59e245fb7575",
        session_id="sess-1",
        reasoning_summary="cast the discount code straight to int",
        risk_flags=["unvalidated-input"],
        model="claude-opus-4",
        tool="claude-code",
        decision_id="dec_abc",
    )
    base.update(over)
    return ProvenanceRecord(**base)


def _resp(introducing: str, record: ProvenanceRecord) -> ResolveResponse:
    return ResolveResponse(resolved=True, introducing_commit=introducing, record=record)


def _req(commit: str = "abc123def456", line: int = 43) -> ResolveRequest:
    return ResolveRequest(commit_sha=commit, file_path="app/payment.py", line=line)


def test_unresolved_scores_nothing() -> None:
    score, factors = attribution_confidence(
        ResolveResponse(resolved=False, detail="no provenance"), _req()
    )
    assert score is None and factors is None


def test_exact_commit_tight_range_is_high() -> None:
    # Deployed commit == introducing commit, a tight range, crash line dead centre.
    score, factors = attribution_confidence(
        _resp("abc123def456", _record(40, 46)), _req(line=43)
    )
    assert score is not None and score >= 0.80
    assert factors is not None
    assert factors["label"] == "high"
    assert factors["match"] == "exact-commit"
    assert factors["position"] == "central"


def test_blame_match_is_discounted_below_exact() -> None:
    rec = _record(40, 46)
    exact, _ = attribution_confidence(_resp("abc123def456", rec), _req(line=43))
    blame, factors = attribution_confidence(_resp("999deadbeef0", rec), _req(line=43))
    assert exact is not None and blame is not None
    assert blame < exact
    assert factors is not None and factors["match"] == "git-blame"


def test_single_line_range_is_maximally_specific() -> None:
    score, factors = attribution_confidence(
        _resp("abc123def456", _record(42, 42)), _req(line=42)
    )
    assert factors is not None and factors["position"] == "single-line"
    assert score == 1.0  # exact-commit, single line, dead-on


def test_wide_range_with_edge_line_lands_low_or_medium() -> None:
    # A 120-line decision that merely contains the crash near its edge, reached via blame.
    score, factors = attribution_confidence(
        _resp("999deadbeef0", _record(1, 120)), _req(line=118)
    )
    assert score is not None and score < 0.60
    assert factors is not None
    assert factors["position"] == "edge"
    assert factors["label"] in {"low", "medium"}


def test_score_is_bounded_zero_to_one() -> None:
    for width in (1, 5, 40, 200):
        for intro in ("abc123def456", "999deadbeef0"):
            score, _ = attribution_confidence(
                _resp(intro, _record(10, 10 + width - 1)), _req(line=10)
            )
            assert score is not None and 0.0 <= score <= 1.0
