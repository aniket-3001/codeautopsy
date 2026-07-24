"""Tests for the authenticated, tenant-scoped /v1 API (the hosted multi-tenant SaaS surface).

The critical property under test is isolation: two orgs, each with their own API key and
dashboard session, must never be able to read or resolve each other's decisions.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codeautopsy.config import Settings
from codeautopsy.provenance.service import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"),
        CODEAUTOPSY_ACCOUNTS_DB=str(tmp_path / "accounts.db"),
        DATABASE_URL=None,
        JWT_SECRET="test-secret-at-least-32-bytes-long-for-hs256",
    )
    return TestClient(create_app(settings))


def _signup(client: TestClient, email: str, password: str = "hunter2pass") -> dict:
    r = client.post("/v1/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


# --- signup / login ---------------------------------------------------------------------


def test_signup_returns_token_and_org(tmp_path: Path):
    client = _client(tmp_path)
    body = _signup(client, "dev@example.com")
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["org_id"]


def test_signup_duplicate_email_is_conflict(tmp_path: Path):
    client = _client(tmp_path)
    _signup(client, "dev@example.com")
    r = client.post("/v1/auth/signup", json={"email": "dev@example.com", "password": "hunter2pass"})
    assert r.status_code == 409


def test_signup_rejects_short_password(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/v1/auth/signup", json={"email": "dev@example.com", "password": "short"})
    assert r.status_code == 422


def test_login_success_and_failure(tmp_path: Path):
    client = _client(tmp_path)
    _signup(client, "dev@example.com")

    r = client.post("/v1/auth/login", json={"email": "dev@example.com", "password": "hunter2pass"})
    assert r.status_code == 200
    assert r.json()["access_token"]

    r = client.post("/v1/auth/login", json={"email": "dev@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_valid_token(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]

    r = client.get("/v1/me", headers=_auth_headers(token))
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "dev@example.com"

    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers=_auth_headers("garbage")).status_code == 401


# --- API keys ---------------------------------------------------------------------------


def test_key_lifecycle(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    headers = _auth_headers(token)

    r = client.post("/v1/keys", headers=headers)
    assert r.status_code == 201
    key = r.json()
    assert key["key"].startswith("ca_live_")

    r = client.get("/v1/keys", headers=headers)
    assert len(r.json()) == 1
    assert r.json()[0]["prefix"] == key["prefix"]

    r = client.delete(f"/v1/keys/{key['id']}", headers=headers)
    assert r.json() == {"revoked": True}
    assert client.get("/v1/keys", headers=headers).json() == []


# --- ingestion + resolve + dashboard, and the isolation invariant -----------------------


def test_ingest_resolve_and_dashboard_round_trip(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    api_key = client.post("/v1/keys", headers=_auth_headers(token)).json()["key"]

    r = client.post(
        "/v1/provenance", json=_record_payload(), headers={"X-Api-Key": api_key}
    )
    assert r.status_code == 201

    r = client.post(
        "/v1/resolve",
        json={
            "commit_sha": "abc123",
            "file_path": "app/payment.py",
            "line": 42,
            "exc_type": "ValueError",
            "exc_message": "invalid literal for int()",
            "blast_radius": 3,
        },
        headers={"X-Api-Key": api_key},
    )
    assert r.status_code == 200
    assert r.json()["resolved"] is True

    r = client.get("/v1/dashboard", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["decision_count"] == 1
    assert body["decisions"][0]["decision_id"] == "dec_7f3a"
    assert body["incident_count"] == 1
    assert body["resolved_incident_count"] == 1
    incident = body["incidents"][0]
    assert incident["exc_type"] == "ValueError"
    assert incident["resolved"] is True
    assert incident["decision_id"] == "dec_7f3a"
    assert incident["blast_radius"] == 3


def test_resolve_persists_crash_trace_ids_for_signoz_deeplink(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    api_key = client.post("/v1/keys", headers=_auth_headers(token)).json()["key"]

    r = client.post(
        "/v1/resolve",
        json={
            "commit_sha": "abc123",
            "file_path": "app/payment.py",
            "line": 42,
            "crash_trace_id": "dad1a6c62838f2369ceaa36846a684a9",
            "crash_span_id": "b2ad89f995e4b170",
        },
        headers={"X-Api-Key": api_key},
    )
    assert r.status_code == 200

    incident = client.get("/v1/dashboard", headers=_auth_headers(token)).json()["incidents"][0]
    assert incident["crash_trace_id"] == "dad1a6c62838f2369ceaa36846a684a9"
    assert incident["crash_span_id"] == "b2ad89f995e4b170"


def test_resolve_records_unresolved_incident_when_no_decision_matches(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    api_key = client.post("/v1/keys", headers=_auth_headers(token)).json()["key"]

    r = client.post(
        "/v1/resolve",
        json={
            "commit_sha": "nomatch",
            "file_path": "app/nowhere.py",
            "line": 1,
            "exc_type": "KeyError",
        },
        headers={"X-Api-Key": api_key},
    )
    assert r.status_code == 200
    assert r.json()["resolved"] is False

    dash = client.get("/v1/dashboard", headers=_auth_headers(token)).json()
    assert dash["incident_count"] == 1
    assert dash["resolved_incident_count"] == 0
    assert dash["incidents"][0]["resolved"] is False
    assert dash["incidents"][0]["decision_id"] is None


def test_two_orgs_cannot_see_each_others_incidents(tmp_path: Path):
    client = _client(tmp_path)

    token_a = _signup(client, "inc-a@example.com")["access_token"]
    key_a = client.post("/v1/keys", headers=_auth_headers(token_a)).json()["key"]

    token_b = _signup(client, "inc-b@example.com")["access_token"]
    key_b = client.post("/v1/keys", headers=_auth_headers(token_b)).json()["key"]

    client.post(
        "/v1/resolve",
        json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42},
        headers={"X-Api-Key": key_a},
    )
    client.post(
        "/v1/resolve",
        json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42},
        headers={"X-Api-Key": key_b},
    )

    dash_a = client.get("/v1/dashboard", headers=_auth_headers(token_a)).json()
    assert dash_a["incident_count"] == 1

    dash_b = client.get("/v1/dashboard", headers=_auth_headers(token_b)).json()
    assert dash_b["incident_count"] == 1


def test_ingest_bulk_scopes_every_record_to_the_authenticated_org(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    api_key = client.post("/v1/keys", headers=_auth_headers(token)).json()["key"]

    payload = [
        _record_payload(decision_id="dec_1"),
        _record_payload(decision_id="dec_2", org_id="someone-elses-org"),
    ]
    r = client.post("/v1/provenance/bulk", json=payload, headers={"X-Api-Key": api_key})
    assert r.status_code == 201
    assert r.json() == {"added": 2, "records": 2}

    dash = client.get("/v1/dashboard", headers=_auth_headers(token)).json()
    assert dash["decision_count"] == 2
    assert {d["decision_id"] for d in dash["decisions"]} == {"dec_1", "dec_2"}


def test_ingest_rejects_missing_or_invalid_api_key(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/v1/provenance", json=_record_payload())
    assert r.status_code == 401

    r = client.post(
        "/v1/provenance", json=_record_payload(), headers={"X-Api-Key": "ca_live_bogus"}
    )
    assert r.status_code == 401


def test_two_orgs_cannot_see_each_others_data(tmp_path: Path):
    """The #1 security invariant from ARCHITECTURE.md: tenant isolation."""
    client = _client(tmp_path)

    token_a = _signup(client, "org-a@example.com")["access_token"]
    key_a = client.post("/v1/keys", headers=_auth_headers(token_a)).json()["key"]

    token_b = _signup(client, "org-b@example.com")["access_token"]
    key_b = client.post("/v1/keys", headers=_auth_headers(token_b)).json()["key"]

    # Both orgs happen to record a decision at the exact same commit/file/line.
    client.post(
        "/v1/provenance",
        json=_record_payload(decision_id="dec_a", reasoning_summary="org A's decision"),
        headers={"X-Api-Key": key_a},
    )
    client.post(
        "/v1/provenance",
        json=_record_payload(decision_id="dec_b", reasoning_summary="org B's decision"),
        headers={"X-Api-Key": key_b},
    )

    # Org A's dashboard only shows org A's decision.
    dash_a = client.get("/v1/dashboard", headers=_auth_headers(token_a)).json()
    assert dash_a["decision_count"] == 1
    assert dash_a["decisions"][0]["decision_id"] == "dec_a"

    dash_b = client.get("/v1/dashboard", headers=_auth_headers(token_b)).json()
    assert dash_b["decision_count"] == 1
    assert dash_b["decisions"][0]["decision_id"] == "dec_b"

    # Org A's key resolving the shared commit/file/line only ever gets org A's decision.
    resolve_a = client.post(
        "/v1/resolve",
        json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42},
        headers={"X-Api-Key": key_a},
    ).json()
    assert resolve_a["record"]["decision_id"] == "dec_a"

    # Org A cannot delete org B's decision even knowing its decision_id.
    r = client.delete("/v1/provenance/dec_b", headers=_auth_headers(token_a))
    assert r.json()["deleted"] == 0
    dash_b_after = client.get("/v1/dashboard", headers=_auth_headers(token_b)).json()
    assert dash_b_after["decision_count"] == 1

    # An org_id smuggled in the request body is ignored — always the authenticated key's org.
    client.post(
        "/v1/provenance",
        json=_record_payload(decision_id="dec_spoof", org_id=dash_b["org_id"]),
        headers={"X-Api-Key": key_a},
    )
    dash_b_final = client.get("/v1/dashboard", headers=_auth_headers(token_b)).json()
    assert dash_b_final["decision_count"] == 1  # spoofed record did NOT land in org B


# --- reliability: leaderboard + risk gate -----------------------------------------------


def test_leaderboard_requires_auth_and_ranks_the_orgs_own_data(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    api_key = client.post("/v1/keys", headers=_auth_headers(token)).json()["key"]

    # One risky decision that crashes, one clean decision that doesn't.
    client.post(
        "/v1/provenance",
        json=_record_payload(decision_id="risky", tool="claude-code", model="opus"),
        headers={"X-Api-Key": api_key},
    )
    client.post(
        "/v1/provenance",
        json=_record_payload(
            decision_id="clean", tool="claude-code", model="opus", risk_flags=[]
        ),
        headers={"X-Api-Key": api_key},
    )
    client.post(
        "/v1/resolve",
        json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42},
        headers={"X-Api-Key": api_key},
    )

    assert client.get("/v1/leaderboard").status_code == 401

    board = client.get("/v1/leaderboard", headers=_auth_headers(token)).json()
    assert board["total_decisions"] == 2
    assert board["total_incidents"] == 1
    row = board["scores"][0]
    assert row["tool"] == "claude-code"
    assert row["model"] == "opus"
    assert row["decisions"] == 2
    assert row["crashed_decisions"] == 1
    assert row["crash_rate"] == 0.5


def test_risk_gate_prices_a_snippet_against_the_orgs_history(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    api_key = client.post("/v1/keys", headers=_auth_headers(token)).json()["key"]

    # Build a track record: 2 decisions carry assumed_valid_input, 1 crashed -> 50%.
    for did in ("h1", "h2"):
        client.post(
            "/v1/provenance", json=_record_payload(decision_id=did), headers={"X-Api-Key": api_key}
        )
    client.post(
        "/v1/resolve",
        json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42},
        headers={"X-Api-Key": api_key},
    )

    assert client.post("/v1/risk-gate", json={"code": "x = 1"}).status_code == 401

    r = client.post(
        "/v1/risk-gate",
        json={"code": "# assume the payload is always valid\nx = int(code)"},
        headers=_auth_headers(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "priced"
    assert body["worst_flag"] == "assumed_valid_input"
    assert body["crash_rate"] == 0.5


def test_risk_gate_clear_snippet(tmp_path: Path):
    client = _client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    r = client.post(
        "/v1/risk-gate", json={"code": "def add(a, b):\n    return a + b"},
        headers=_auth_headers(token),
    )
    assert r.json()["verdict"] == "clear"


# --- Auto-Heal loop ---------------------------------------------------------------------

_HEAL_SECRET = "test-heal-secret-32-bytes-xxxxxxxxxxxx"


def _heal_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        CODEAUTOPSY_PROVENANCE_DB=str(tmp_path / "p.db"),
        CODEAUTOPSY_ACCOUNTS_DB=str(tmp_path / "accounts.db"),
        DATABASE_URL=None,
        JWT_SECRET="test-secret-at-least-32-bytes-long-for-hs256",
        HEAL_WEBHOOK_SECRET=_HEAL_SECRET,
    )
    return TestClient(create_app(settings))


def test_heal_trigger_creates_a_run_in_the_callers_org(tmp_path: Path):
    client = _heal_client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    r = client.post("/v1/heal/trigger", json={}, headers=_auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    # No token configured in tests -> dispatch is a graceful no-op, run still recorded.
    assert body["status"] == "dispatch_failed"
    assert body["trigger"] == "manual"
    assert body["file_path"].endswith("sample_app/main.py")
    assert body["events"]


def test_heal_trigger_requires_auth(tmp_path: Path):
    client = _heal_client(tmp_path)
    assert client.post("/v1/heal/trigger", json={}).status_code == 401


def test_heal_runs_are_org_scoped(tmp_path: Path):
    client = _heal_client(tmp_path)
    tok_a = _signup(client, "a@example.com")["access_token"]
    tok_b = _signup(client, "b@example.com")["access_token"]
    client.post("/v1/heal/trigger", json={}, headers=_auth_headers(tok_a))

    runs_a = client.get("/v1/heal/runs", headers=_auth_headers(tok_a)).json()["runs"]
    runs_b = client.get("/v1/heal/runs", headers=_auth_headers(tok_b)).json()["runs"]
    assert len(runs_a) == 1
    assert runs_b == []


def test_heal_webhook_requires_the_shared_secret(tmp_path: Path):
    client = _heal_client(tmp_path)
    # Wrong secret is rejected.
    bad = client.post(
        "/v1/heal/webhook", json={"org_id": "demo-public"}, headers={"X-Heal-Secret": "nope"}
    )
    assert bad.status_code == 401
    # Correct secret creates a signoz-alert run.
    ok = client.post(
        "/v1/heal/webhook",
        json={"org_id": "demo-public", "alert": "crash storm"},
        headers={"X-Heal-Secret": _HEAL_SECRET},
    )
    assert ok.status_code == 200
    assert ok.json()["trigger"] == "signoz-alert"


def test_heal_complete_reports_the_pr_back(tmp_path: Path):
    client = _heal_client(tmp_path)
    token = _signup(client, "dev@example.com")["access_token"]
    run = client.post("/v1/heal/trigger", json={}, headers=_auth_headers(token)).json()

    # Fix Bot reports back (shared-secret authed) with the opened PR.
    r = client.post(
        f"/v1/heal/{run['run_id']}/complete",
        json={
            "org_id": run["org_id"],
            "status": "succeeded",
            "pr_url": "https://github.com/x/y/pull/9",
            "explanation": "validated the discount code",
        },
        headers={"X-Heal-Secret": _HEAL_SECRET},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    assert r.json()["pr_url"] == "https://github.com/x/y/pull/9"

    # And it now shows on the org's runs feed.
    runs = client.get("/v1/heal/runs", headers=_auth_headers(token)).json()["runs"]
    assert runs[0]["status"] == "succeeded"


def test_heal_complete_unknown_run_is_404(tmp_path: Path):
    client = _heal_client(tmp_path)
    r = client.post(
        "/v1/heal/heal_missing/complete",
        json={"org_id": "demo-public", "status": "succeeded"},
        headers={"X-Heal-Secret": _HEAL_SECRET},
    )
    assert r.status_code == 404


def test_legacy_public_endpoints_still_work_unauthenticated(tmp_path: Path):
    """The scripted sandbox demo (docs/demo.html) has no auth and must be unaffected."""
    client = _client(tmp_path)
    r = client.post("/provenance", json=_record_payload())
    assert r.status_code == 201

    r = client.post(
        "/resolve", json={"commit_sha": "abc123", "file_path": "app/payment.py", "line": 42}
    )
    assert r.status_code == 200
    assert r.json()["resolved"] is True
