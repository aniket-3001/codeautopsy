"""Tests for the sample app: the seeded bug crashes reliably and routes into the enricher."""

from __future__ import annotations

from fastapi.testclient import TestClient

import codeautopsy.sample_app.main as sample_main
from codeautopsy.provenance.models import ResolveResponse


def test_health():
    client = TestClient(sample_main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_checkout_success():
    client = TestClient(sample_main.app)
    r = client.post("/checkout", json={"discount_code": "10", "subtotal": 100})
    assert r.status_code == 200
    assert r.json() == {"total": 90, "discount_applied": 10}


def test_checkout_seeded_bug_triggers_autopsy(monkeypatch):
    calls = []

    def fake_autopsy(exc, *, commit_sha, file_path, line, blast_radius=1, settings=None, **_kwargs):
        calls.append(
            {"file_path": file_path, "line": line, "exc": str(exc), "commit_sha": commit_sha}
        )
        return ResolveResponse(resolved=False, detail="test stub")

    monkeypatch.setattr(sample_main, "autopsy_exception", fake_autopsy)

    client = TestClient(sample_main.app)
    r = client.post("/checkout", json={"discount_code": "GIMME50", "subtotal": 100})

    assert r.status_code == 500
    assert len(calls) == 1
    assert "sample_app/main.py" in calls[0]["file_path"]
    assert "invalid literal" in calls[0]["exc"]
    assert calls[0]["commit_sha"] == sample_main.DEPLOYED_COMMIT_SHA


def test_checkout_bug_response_carries_codeautopsy_payload(monkeypatch):
    from codeautopsy.provenance.models import ProvenanceRecord

    rec = ProvenanceRecord(
        commit_sha="abc",
        file_path="codeautopsy/sample_app/main.py",
        line_start=1,
        line_end=1,
        decision_span_id="e91ca75cd1ae81e4",
        decision_trace_id="c51641b768a8a67ea979f9005ade2f55",
        session_id="s",
        reasoning_summary="assuming input is always valid",
        risk_flags=["assumed_valid_input"],
        decision_id="dec_1",
    )

    def fake_autopsy(exc, *, commit_sha, file_path, line, blast_radius=1, settings=None, **_kwargs):
        return ResolveResponse(resolved=True, introducing_commit="abc", record=rec)

    monkeypatch.setattr(sample_main, "autopsy_exception", fake_autopsy)

    client = TestClient(sample_main.app)
    r = client.post("/checkout", json={"discount_code": "not-a-number"})
    body = r.json()["detail"]
    assert body["codeautopsy"]["resolved"] is True
    assert body["codeautopsy"]["decision_summary"] == "assuming input is always valid"
    assert body["codeautopsy"]["risk_flags"] == ["assumed_valid_input"]


def test_blast_radius_increments_across_repeated_crashes(monkeypatch):
    calls = []

    def fake_autopsy(exc, *, commit_sha, file_path, line, blast_radius=1, settings=None, **_kwargs):
        calls.append(blast_radius)
        return ResolveResponse(resolved=False, detail="test stub")

    monkeypatch.setattr(sample_main, "autopsy_exception", fake_autopsy)
    sample_main._crash_counts.clear()

    client = TestClient(sample_main.app)
    for _ in range(3):
        client.post("/checkout", json={"discount_code": "bad"})

    assert calls == [1, 2, 3]
