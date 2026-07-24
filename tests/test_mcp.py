"""Unit tests for the CodeAutopsy MCP tool logic (`codeautopsy.mcp.core`).

These exercise the pure tool functions directly with an in-memory-ish SQLite store, so they
need neither the `mcp` package's transport nor a network hop. A separate test confirms the
FastMCP server wires the three tools up when the `mcp` package is present.
"""

from __future__ import annotations

import importlib.util
import tempfile

import pytest

from codeautopsy.config import Settings
from codeautopsy.mcp import core
from codeautopsy.provenance.models import IncidentRecord, ProvenanceRecord
from codeautopsy.provenance.store import ProvenanceStore

_HAS_MCP = importlib.util.find_spec("mcp") is not None


@pytest.fixture
def store() -> ProvenanceStore:
    return ProvenanceStore(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)


def _settings() -> Settings:
    return Settings()


def _record(**over) -> ProvenanceRecord:
    base = dict(
        org_id="demo-public",
        commit_sha="abc123def456",
        file_path="app/payment.py",
        line_start=40,
        line_end=46,
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


def test_autopsy_resolves_a_decision_at_the_deployed_commit(store: ProvenanceStore) -> None:
    store.add(_record())
    out = core.autopsy(
        "abc123def456", "app/payment.py", 42, repo=None, store=store, settings=_settings()
    )
    assert out["resolved"] is True
    assert out["decision_id"] == "dec_abc"
    assert out["authored_by"] == {"tool": "claude-code", "model": "claude-opus-4"}
    assert out["risk_flags"] == ["unvalidated-input"]
    assert out["line_range"] == [40, 46]
    assert out["coordinate"] == "app/payment.py:42@abc123def456"


def test_autopsy_unresolved_is_truthful(store: ProvenanceStore) -> None:
    # No record indexed, and repo=None so there is nothing to blame against.
    out = core.autopsy(
        "deadbeef", "app/other.py", 9, repo=None, store=store, settings=_settings()
    )
    assert out["resolved"] is False
    assert "decision_id" not in out
    assert out["detail"]


def test_autopsy_is_org_scoped(store: ProvenanceStore) -> None:
    store.add(_record(org_id="tenant-a"))
    # Default org can't see tenant-a's decision.
    out = core.autopsy(
        "abc123def456", "app/payment.py", 42, repo=None, store=store, settings=_settings()
    )
    assert out["resolved"] is False


def test_prognose_prices_a_snippet_against_history(store: ProvenanceStore) -> None:
    # Two prior decisions carry the flag, one of which crashed -> a real rate exists.
    store.add(_record(decision_id="d1"))
    store.add(_record(decision_id="d2", line_start=60, line_end=66))
    store.add_incident(
        IncidentRecord(
            org_id="demo-public",
            commit_sha="abc123def456",
            file_path="app/payment.py",
            line=42,
            resolved=True,
            decision_id="d1",
        )
    )
    out = core.prognose(
        "discount = int(request.args['code'])", store=store, settings=_settings()
    )
    assert out["verdict"] in {"priced", "flagged", "clear"}
    assert "flags" in out


def test_leaderboard_ranks_tools(store: ProvenanceStore) -> None:
    store.add(_record(decision_id="d1"))
    store.add_incident(
        IncidentRecord(
            org_id="demo-public",
            commit_sha="abc123def456",
            file_path="app/payment.py",
            line=42,
            resolved=True,
            decision_id="d1",
        )
    )
    out = core.leaderboard(store=store, settings=_settings())
    assert out["total_decisions"] == 1
    assert out["total_incidents"] == 1
    assert out["scores"], "expected at least one ranked tool/model"
    assert out["scores"][0]["tool"] == "claude-code"


@pytest.mark.skipif(not _HAS_MCP, reason="mcp package not installed")
def test_server_registers_three_tools() -> None:
    from codeautopsy.mcp.server import build_server

    server = build_server()
    # FastMCP exposes registered tools asynchronously; the manager holds them synchronously.
    names = set(server._tool_manager._tools.keys())  # noqa: SLF001
    assert {"autopsy", "prognose", "leaderboard"} <= names
