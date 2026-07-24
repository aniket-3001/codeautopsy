"""Tests for the Reliability lens: the leaderboard and the risk gate.

Same real-ProvenanceStore fixtures as test_prognosis.py — both features are just aggregate
re-projections of the exact provenance + incidents join Prognosis prices, so they're tested
against the same kind of data: recorded decisions carrying risk flags, plus incidents that
attribute crashes back to some of them.
"""

from __future__ import annotations

from pathlib import Path

from codeautopsy.provenance.models import IncidentRecord, ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStore
from codeautopsy.reliability.core import compute_leaderboard, score_snippet


def _record(decision_id: str, *, tool: str, model: str, flags: list[str]) -> ProvenanceRecord:
    return ProvenanceRecord(
        commit_sha=f"c_{decision_id}",
        file_path="app.py",
        line_start=1,
        line_end=1,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="sess_test",
        reasoning_summary="assuming the input is always valid",
        risk_flags=flags,
        tool=tool,
        model=model,
        decision_id=decision_id,
    )


def _incident(decision_id: str) -> IncidentRecord:
    return IncidentRecord(
        commit_sha=f"c_{decision_id}", file_path="app.py", line=1, decision_id=decision_id
    )


# --- leaderboard --------------------------------------------------------------------------------


def test_leaderboard_groups_by_tool_and_model(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("d1", tool="claude-code", model="opus", flags=["assumed_valid_input"]))
    store.add(_record("d2", tool="claude-code", model="opus", flags=["todo_left"]))
    store.add(_record("d3", tool="cursor", model="gpt-4", flags=["skipped_tests"]))

    board = compute_leaderboard(store)
    keys = {(s.tool, s.model) for s in board.scores}
    assert keys == {("claude-code", "opus"), ("cursor", "gpt-4")}
    assert board.total_decisions == 3


def test_leaderboard_computes_crash_rate_and_incidents_caused(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    # claude-code/opus: 2 decisions, 1 crashed (twice).
    store.add(_record("d1", tool="claude-code", model="opus", flags=["assumed_valid_input"]))
    store.add(_record("d2", tool="claude-code", model="opus", flags=["todo_left"]))
    store.add_incident(_incident("d1"))
    store.add_incident(_incident("d1"))  # same decision crashed twice -> 2 incidents, still 1 crashed decision

    board = compute_leaderboard(store)
    row = next(s for s in board.scores if s.tool == "claude-code")
    assert row.decisions == 2
    assert row.crashed_decisions == 1
    assert row.crash_rate == 0.5
    assert row.incidents_caused == 2
    assert board.total_incidents == 2


def test_leaderboard_ranks_worst_crash_rate_first(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    # safe-tool: 2 decisions, 0 crashed -> rate 0.0
    store.add(_record("s1", tool="safe-tool", model="m", flags=["todo_left"]))
    store.add(_record("s2", tool="safe-tool", model="m", flags=["todo_left"]))
    # risky-tool: 2 decisions, 2 crashed -> rate 1.0
    store.add(_record("r1", tool="risky-tool", model="m", flags=["assumed_valid_input"]))
    store.add(_record("r2", tool="risky-tool", model="m", flags=["assumed_valid_input"]))
    store.add_incident(_incident("r1"))
    store.add_incident(_incident("r2"))

    board = compute_leaderboard(store)
    assert board.scores[0].tool == "risky-tool"
    assert board.scores[0].crash_rate == 1.0
    assert board.scores[-1].tool == "safe-tool"


def test_leaderboard_surfaces_worst_flag(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    # Within one tool/model: assumed_valid_input crashes, todo_left never does.
    store.add(_record("a1", tool="t", model="m", flags=["assumed_valid_input"]))
    store.add(_record("a2", tool="t", model="m", flags=["assumed_valid_input"]))
    store.add(_record("b1", tool="t", model="m", flags=["todo_left"]))
    store.add_incident(_incident("a1"))
    store.add_incident(_incident("a2"))

    board = compute_leaderboard(store)
    row = board.scores[0]
    assert row.worst_flag == "assumed_valid_input"
    assert row.worst_flag_rate == 1.0


def test_leaderboard_falls_back_to_unknown_labels(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    store.add(_record("d1", tool="", model="", flags=[]))
    board = compute_leaderboard(store)
    assert board.scores[0].tool == "unknown"
    assert board.scores[0].model == "unknown"


def test_leaderboard_empty_store(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    board = compute_leaderboard(store)
    assert board.scores == []
    assert board.total_decisions == 0


def test_leaderboard_is_org_scoped(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    mine = _record("d1", tool="mine", model="m", flags=["todo_left"])
    mine.org_id = "org_a"
    theirs = _record("d2", tool="theirs", model="m", flags=["todo_left"])
    theirs.org_id = "org_b"
    store.add(mine)
    store.add(theirs)

    board = compute_leaderboard(store, org_id="org_a")
    assert [s.tool for s in board.scores] == ["mine"]


# --- risk gate ----------------------------------------------------------------------------------


def test_risk_gate_clear_on_boring_code(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    resp = score_snippet(store, "def add(a, b):\n    return a + b\n")
    assert resp.verdict == "clear"
    assert resp.flags == []


def test_risk_gate_flagged_without_history(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")  # empty history — nothing to price against
    resp = score_snippet(store, "# TODO: validate this later\nx = int(code)")
    assert resp.verdict == "flagged"
    assert any(f.flag == "todo_left" for f in resp.flags)
    assert resp.crash_rate is None
    # No history -> every flag is unpriced.
    assert all(f.crash_rate is None for f in resp.flags)


def test_risk_gate_prices_against_org_history(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    # 4 historical decisions carry assumed_valid_input; 3 crashed -> 75%.
    for i in range(4):
        store.add(_record(f"h{i}", tool="t", model="m", flags=["assumed_valid_input"]))
    for i in range(3):
        store.add_incident(_incident(f"h{i}"))

    resp = score_snippet(store, "# we can assume the payload is always valid here")
    assert resp.verdict == "priced"
    assert resp.worst_flag == "assumed_valid_input"
    assert resp.crash_rate == 0.75
    assert resp.sample_size == 4


def test_risk_gate_detects_flags_in_reasoning_too(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    resp = score_snippet(store, code="x = 1", reasoning="I'll skip the tests for now")
    assert any(f.flag == "skipped_tests" for f in resp.flags)


def test_risk_gate_is_org_scoped(tmp_path: Path):
    store = ProvenanceStore(tmp_path / "p.db")
    # History lives in org_b; scoring for org_a must not see it -> flagged, not priced.
    for i in range(4):
        rec = _record(f"h{i}", tool="t", model="m", flags=["assumed_valid_input"])
        rec.org_id = "org_b"
        store.add(rec)
        inc = _incident(f"h{i}")
        inc.org_id = "org_b"
        store.add_incident(inc)

    resp = score_snippet(store, "assume input is always valid", org_id="org_a")
    assert resp.verdict == "flagged"
    assert resp.crash_rate is None
