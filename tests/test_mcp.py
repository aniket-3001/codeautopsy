"""Unit tests for the CodeAutopsy MCP tool logic (`codeautopsy.mcp.core`).

These exercise the pure tool functions directly with an in-memory-ish SQLite store, so they
need neither the `mcp` package's transport nor a network hop. A separate test confirms the
FastMCP server wires the five tools up when the `mcp` package is present.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from codeautopsy.config import Settings
from codeautopsy.enricher.incidents import append_incident
from codeautopsy.fixbot import core as fixbot_core
from codeautopsy.fixbot.lessons import record_lesson
from codeautopsy.mcp import core
from codeautopsy.provenance.models import IncidentRecord, ProvenanceRecord, ResolveResponse
from codeautopsy.provenance.store import ProvenanceStore

_HAS_MCP = importlib.util.find_spec("mcp") is not None


@pytest.fixture
def store() -> ProvenanceStore:
    return ProvenanceStore(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)


def _memory_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _settings() -> Settings:
    return Settings()


def _settings_for_repo(repo: Path) -> Settings:
    return Settings(CODEAUTOPSY_TARGET_REPO=str(repo))


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
    # Attribution confidence rides along on the payload — exact-commit hit here.
    assert isinstance(out["confidence"], float)
    assert out["confidence_factors"]["match"] == "exact-commit"
    assert out["confidence_factors"]["label"] in {"high", "medium", "low"}


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


def test_verify_provenance_valid_chain(store: ProvenanceStore) -> None:
    store.add(_record(decision_id="d1"))
    store.add(_record(decision_id="d2", line_start=50, line_end=55))
    out = core.verify_provenance(store=store, settings=_settings())
    assert out["valid"] is True
    assert out["length"] == 2
    assert out["broken_at"] is None


def test_verify_provenance_empty_chain(store: ProvenanceStore) -> None:
    out = core.verify_provenance(store=store, settings=_settings())
    assert out["valid"] is True
    assert out["length"] == 0


def test_verify_provenance_detects_tampering(store: ProvenanceStore) -> None:
    """A row edited directly in the database (bypassing `store.add()`) must be caught —
    that's the whole point of the hash chain: it isn't the write path that's trusted, it's
    the recomputation."""
    store.add(_record(decision_id="d1"))
    with store._conn() as conn:  # noqa: SLF001 — reaching past the API is the point of this test
        conn.execute(
            "UPDATE provenance SET reasoning_summary = ? WHERE decision_id = ?",
            ("this was never actually said", "d1"),
        )
    out = core.verify_provenance(store=store, settings=_settings())
    assert out["valid"] is False
    assert out["broken_at"] == "d1"


def test_postmortem_renders_full_case_file_with_lesson(
    store: ProvenanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path
    (repo / "app").mkdir()
    (repo / "app" / "payment.py").write_text(
        "def checkout(request):\n    return int(request.args['code'])\n", encoding="utf-8"
    )
    rec = _record()
    monkeypatch.setattr(
        fixbot_core,
        "resolve_decision",
        lambda *a, **k: ResolveResponse(
            resolved=True,
            introducing_commit="abc123def456",
            record=rec,
            confidence=1.0,
            confidence_factors={"label": "high", "match": "exact-commit"},
        ),
    )
    append_incident(
        repo,
        {
            "file_path": "app/payment.py",
            "line": 42,
            "exc_type": "ValueError",
            "exc_message": "invalid literal for int() with base 10: 'GIMME50'",
            "cause_of_death": "invalid value — unvalidated input",
            "resolved": True,
            "decision_id": "dec_abc",
        },
    )
    record_lesson(
        store,
        lesson="always validate external input before int()",
        cause_of_death="invalid value — unvalidated input",
        file_path="app/payment.py",
        risk_flags=["unvalidated-input"],
        decision_id="dec_abc",
        patch_summary="wrapped in try/except, defaults to 0",
    )

    out = core.postmortem(
        "abc123def456", "app/payment.py", 42, store=store, settings=_settings_for_repo(repo)
    )

    markdown = out["markdown"]
    assert "app/payment.py:42" in markdown
    assert "cast the discount code straight to int" in markdown
    assert "always validate external input before int()" in markdown
    assert "Seen **1x**" in markdown


def test_postmortem_renders_when_unresolved_and_no_lesson(
    store: ProvenanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        fixbot_core,
        "resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=False, detail="no matching decision indexed"),
    )

    out = core.postmortem(
        "deadbeef", "other.py", 9, store=store, settings=_settings_for_repo(repo)
    )

    markdown = out["markdown"]
    assert "No decision indexed for this line" in markdown
    assert "No lesson recorded yet for this class of bug" in markdown


def test_autopsy_span_records_resolved_without_error_status(store: ProvenanceStore) -> None:
    store.add(_record())
    provider, exporter = _memory_provider()
    core.autopsy(
        "abc123def456",
        "app/payment.py",
        42,
        repo=None,
        store=store,
        settings=_settings(),
        tracer_provider=provider,
    )
    span = exporter.get_finished_spans()[0]
    assert span.name == "codeautopsy.mcp.autopsy"
    assert span.attributes["codeautopsy.mcp.resolved"] is True
    assert span.attributes["code.filepath"] == "app/payment.py"
    assert span.status.status_code == StatusCode.UNSET


def test_autopsy_span_records_unresolved_as_attribute_not_as_an_error(
    store: ProvenanceStore,
) -> None:
    """A crash with no matching decision is a legitimate, correct answer — not a bug in the
    tool — so the span must stay OK/UNSET. This is the exact distinction opentel-mcp's
    silent-failure detection misses if you only mark spans on the exception path: it would
    call this a success (no exception raised), which is true at the protocol level but
    hides the semantic outcome from anyone querying traces. Recording the attribute (without
    flipping status to ERROR) makes it visible without crying wolf.
    """
    provider, exporter = _memory_provider()
    core.autopsy(
        "deadbeef",
        "app/other.py",
        9,
        repo=None,
        store=store,
        settings=_settings(),
        tracer_provider=provider,
    )
    span = exporter.get_finished_spans()[0]
    assert span.attributes["codeautopsy.mcp.resolved"] is False
    assert span.status.status_code == StatusCode.UNSET


def test_autopsy_span_records_exception_and_reraises(
    store: ProvenanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(core, "resolve_provenance", _raise)
    provider, exporter = _memory_provider()

    with pytest.raises(RuntimeError, match="store exploded"):
        core.autopsy(
            "abc123def456",
            "app/payment.py",
            42,
            repo=None,
            store=store,
            settings=_settings(),
            tracer_provider=provider,
        )

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_prognose_span_records_verdict(store: ProvenanceStore) -> None:
    provider, exporter = _memory_provider()
    core.prognose(
        "discount = int(request.args['code'])",
        store=store,
        settings=_settings(),
        tracer_provider=provider,
    )
    span = exporter.get_finished_spans()[0]
    assert span.name == "codeautopsy.mcp.prognose"
    assert span.attributes["codeautopsy.mcp.verdict"] in {"priced", "flagged", "clear"}
    assert span.attributes["codeautopsy.mcp.snippet_length"] == len(
        "discount = int(request.args['code'])"
    )


def test_prognose_span_records_exception_and_reraises(
    store: ProvenanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(core, "score_snippet", _raise)
    provider, exporter = _memory_provider()

    with pytest.raises(RuntimeError, match="scoring blew up"):
        core.prognose("int(x)", store=store, settings=_settings(), tracer_provider=provider)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_leaderboard_span_records_exception_and_reraises(
    store: ProvenanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("leaderboard blew up")

    monkeypatch.setattr(core, "compute_leaderboard", _raise)
    provider, exporter = _memory_provider()

    with pytest.raises(RuntimeError, match="leaderboard blew up"):
        core.leaderboard(store=store, settings=_settings(), tracer_provider=provider)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_leaderboard_span_records_totals(store: ProvenanceStore) -> None:
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
    provider, exporter = _memory_provider()
    core.leaderboard(store=store, settings=_settings(), tracer_provider=provider)
    span = exporter.get_finished_spans()[0]
    assert span.name == "codeautopsy.mcp.leaderboard"
    assert span.attributes["codeautopsy.mcp.total_decisions"] == 1
    assert span.attributes["codeautopsy.mcp.total_incidents"] == 1


def test_verify_provenance_span_records_chain_result(store: ProvenanceStore) -> None:
    store.add(_record(decision_id="d1"))
    provider, exporter = _memory_provider()
    core.verify_provenance(store=store, settings=_settings(), tracer_provider=provider)
    span = exporter.get_finished_spans()[0]
    assert span.name == "codeautopsy.mcp.verify_provenance"
    assert span.attributes["codeautopsy.mcp.chain_valid"] is True
    assert span.attributes["codeautopsy.mcp.chain_length"] == 1


def test_verify_provenance_span_records_exception_and_reraises(
    store: ProvenanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(self: ProvenanceStore, org_id: str = "demo-public") -> None:
        raise RuntimeError("verify blew up")

    monkeypatch.setattr(ProvenanceStore, "verify_integrity", _raise)
    provider, exporter = _memory_provider()

    with pytest.raises(RuntimeError, match="verify blew up"):
        core.verify_provenance(store=store, settings=_settings(), tracer_provider=provider)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_postmortem_span_records_success(
    store: ProvenanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        fixbot_core, "resolve_decision", lambda *a, **k: ResolveResponse(resolved=False)
    )
    provider, exporter = _memory_provider()

    core.postmortem(
        "deadbeef",
        "other.py",
        9,
        store=store,
        settings=_settings_for_repo(tmp_path),
        tracer_provider=provider,
    )

    span = exporter.get_finished_spans()[0]
    assert span.name == "codeautopsy.mcp.postmortem"
    assert span.attributes["code.filepath"] == "other.py"
    assert span.attributes["codeautopsy.mcp.lesson_recalled"] is False
    assert span.status.status_code != StatusCode.ERROR


def test_postmortem_span_records_exception_and_reraises(
    store: ProvenanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fixbot_core,
        "resolve_decision",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git blame blew up")),
    )
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    provider, exporter = _memory_provider()

    with pytest.raises(RuntimeError, match="git blame blew up"):
        core.postmortem(
            "deadbeef",
            "other.py",
            9,
            store=store,
            settings=_settings_for_repo(tmp_path),
            tracer_provider=provider,
        )

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


@pytest.mark.skipif(not _HAS_MCP, reason="mcp package not installed")
def test_server_registers_five_tools() -> None:
    from codeautopsy.mcp.server import build_server

    server = build_server()
    # FastMCP exposes registered tools asynchronously; the manager holds them synchronously.
    names = set(server._tool_manager._tools.keys())  # noqa: SLF001
    assert {"autopsy", "prognose", "postmortem", "leaderboard", "verify_provenance"} <= names


@pytest.mark.skipif(not _HAS_MCP, reason="mcp package not installed")
def test_server_tool_wrappers_call_through_to_core(
    store: ProvenanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper functions `build_server()` registers take no store/settings — they let
    `core.autopsy`/`prognose`/`leaderboard` default to `make_store(get_settings())`. Patch
    those two so the call stays on the test's temp store instead of the real provenance.db.
    """
    from codeautopsy.mcp.server import build_server

    store.add(_record())
    monkeypatch.setattr(core, "get_settings", _settings)
    monkeypatch.setattr(core, "make_store", lambda settings: store)

    server = build_server()
    tools = server._tool_manager._tools  # noqa: SLF001

    autopsy_out = tools["autopsy"].fn(
        commit_sha="abc123def456", file_path="app/payment.py", line=42, repo=None
    )
    assert autopsy_out["resolved"] is True
    assert autopsy_out["decision_id"] == "dec_abc"

    prognose_out = tools["prognose"].fn(code="discount = int(request.args['code'])")
    assert prognose_out["verdict"] in {"priced", "flagged", "clear"}

    leaderboard_out = tools["leaderboard"].fn()
    assert leaderboard_out["total_decisions"] == 1

    verify_out = tools["verify_provenance"].fn()
    assert verify_out["valid"] is True
    assert verify_out["length"] == 1


@pytest.mark.skipif(not _HAS_MCP, reason="mcp package not installed")
def test_server_postmortem_tool_wrapper_calls_through_to_core(
    store: ProvenanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`postmortem` needs a real file on disk (`build_genealogy` reads it), so it gets its own
    test with `target_repo` pointed at a temp dir, rather than sharing the fixture-repo-root
    settings the other three wrappers use above.
    """
    from codeautopsy.mcp.server import build_server

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "payment.py").write_text(
        "def checkout(request):\n    return int(request.args['code'])\n", encoding="utf-8"
    )
    rec = _record()
    monkeypatch.setattr(
        fixbot_core,
        "resolve_decision",
        lambda *a, **k: ResolveResponse(resolved=True, introducing_commit="abc123def456", record=rec),
    )
    monkeypatch.setattr(core, "get_settings", lambda: _settings_for_repo(tmp_path))
    monkeypatch.setattr(core, "make_store", lambda settings: store)

    server = build_server()
    tools = server._tool_manager._tools  # noqa: SLF001

    out = tools["postmortem"].fn(commit_sha="abc123def456", file_path="app/payment.py", line=42)

    assert "cast the discount code straight to int" in out["markdown"]
