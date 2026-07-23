"""Tests for the provenance FastAPI service (the enricher's HTTP dependency)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codeautopsy.config import Settings
from codeautopsy.provenance.service import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"))
    return TestClient(create_app(settings))


def _record_payload(**overrides) -> dict:
    base = dict(
        commit_sha="abc123",
        file_path="app/payment.py",
        line_start=40,
        line_end=45,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="sess_test",
        reasoning_summary="assuming the input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_7f3a",
    )
    base.update(overrides)
    return base


def test_health_reports_zero_records_on_empty_store(tmp_path: Path):
    client = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["records"] == 0


def test_add_and_list(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/provenance", json=_record_payload())
    assert r.status_code == 201
    assert r.json() == {"added": True, "records": 1}

    r = client.get("/provenance")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["decision_id"] == "dec_7f3a"


def test_add_bulk(tmp_path: Path):
    client = _client(tmp_path)
    payload = [_record_payload(decision_id="dec_1"), _record_payload(decision_id="dec_2")]
    r = client.post("/provenance/bulk", json=payload)
    assert r.status_code == 201
    assert r.json() == {"added": 2, "records": 2}


def test_resolve_fast_path_over_http(tmp_path: Path):
    client = _client(tmp_path)
    client.post("/provenance", json=_record_payload())

    r = client.post(
        "/resolve", json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is True
    assert body["introducing_commit"] == "abc123"
    assert body["record"]["decision_id"] == "dec_7f3a"


def test_resolve_unresolved_when_no_match(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post(
        "/resolve", json={"commit_sha": "nope", "file_path": "app/payment.py", "line": 1}
    )
    assert r.status_code == 200
    assert r.json()["resolved"] is False
