"""FastAPI provenance service — the `resolve` endpoint the Autopsy Enricher calls.

On a runtime exception, the enricher POSTs (commit, file, line) here and gets back the AI
decision that authored that line, which it then attaches to the autopsy span as a link.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from codeautopsy.accounts.auth import make_require_api_key, make_require_user
from codeautopsy.accounts.models import (
    ApiKeyPublic,
    CreateApiKeyResponse,
    LoginRequest,
    MeResponse,
    SignupRequest,
    TokenResponse,
)
from codeautopsy.accounts.security import create_session_token
from codeautopsy.accounts.store import AccountStore, AccountStoreProtocol, EmailAlreadyRegistered
from codeautopsy.config import Settings, get_settings
from codeautopsy.provenance.indexer import resolve as resolve_provenance
from codeautopsy.provenance.models import (
    IncidentRecord,
    ProvenanceRecord,
    ResolveRequest,
    ResolveResponse,
)
from codeautopsy.provenance.store import ProvenanceStore, ProvenanceStoreProtocol
from codeautopsy.reliability.core import compute_leaderboard, score_snippet
from codeautopsy.reliability.models import (
    LeaderboardReport,
    RiskGateRequest,
    RiskGateResponse,
)

# Public demo: the sandbox page (GitHub Pages) calls this service directly from the browser.
DEMO_ORIGINS = [
    "https://aniket-3001.github.io",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]


def _make_store(settings: Settings) -> ProvenanceStoreProtocol:
    if settings.database_url:
        from codeautopsy.provenance.store_postgres import PostgresProvenanceStore

        return PostgresProvenanceStore(settings.database_url)
    return ProvenanceStore(settings.provenance_db)


def _make_account_store(settings: Settings) -> AccountStoreProtocol:
    if settings.database_url:
        from codeautopsy.accounts.store_postgres import PostgresAccountStore

        return PostgresAccountStore(settings.database_url)
    return AccountStore(settings.accounts_db)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = _make_store(settings)
    accounts = _make_account_store(settings)
    require_user = make_require_user(accounts, settings)
    require_api_key = make_require_api_key(accounts)
    app = FastAPI(title="CodeAutopsy Provenance", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEMO_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        db = "postgres" if settings.database_url else str(settings.provenance_db)
        return {"status": "ok", "records": store.count(), "db": db}

    @app.post("/provenance", status_code=201)
    def add(record: ProvenanceRecord) -> dict:
        store.add(record)
        return {"added": True, "records": store.count()}

    @app.post("/provenance/bulk", status_code=201)
    def add_bulk(records: list[ProvenanceRecord]) -> dict:
        n = store.add_many(records)
        return {"added": n, "records": store.count()}

    @app.get("/provenance", response_model=list[ProvenanceRecord])
    def list_all() -> list[ProvenanceRecord]:
        return store.all()

    @app.delete("/provenance/{decision_id}")
    def delete(decision_id: str) -> dict:
        # Scoped by decision_id (not commit/file/line) so removing one demo submission can
        # never delete someone else's real record for the same crashing line.
        deleted = store.delete(decision_id)
        return {"deleted": deleted, "records": store.count()}

    @app.post("/resolve", response_model=ResolveResponse)
    def resolve(req: ResolveRequest) -> ResolveResponse:
        return resolve_provenance(store, req, repo=settings.target_repo)

    # --- /v1: authenticated, tenant-scoped API for the hosted multi-tenant SaaS -------------

    @app.post("/v1/auth/signup", response_model=TokenResponse, status_code=201)
    def v1_signup(req: SignupRequest) -> TokenResponse:
        if len(req.password) < 8:
            raise HTTPException(status_code=422, detail="password must be at least 8 characters")
        try:
            user, org = accounts.create_user_with_org(req.email, req.password)
        except EmailAlreadyRegistered as exc:
            raise HTTPException(status_code=409, detail="email already registered") from exc
        token = create_session_token(
            user.id, org.id, settings.jwt_secret, settings.jwt_expires_seconds
        )
        return TokenResponse(access_token=token, org_id=org.id)

    @app.post("/v1/auth/login", response_model=TokenResponse)
    def v1_login(req: LoginRequest) -> TokenResponse:
        user = accounts.verify_login(req.email, req.password)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid email or password")
        org = accounts.get_org_for_user(user.id)
        if org is None:
            raise HTTPException(status_code=500, detail="user has no org")
        token = create_session_token(
            user.id, org.id, settings.jwt_secret, settings.jwt_expires_seconds
        )
        return TokenResponse(access_token=token, org_id=org.id)

    @app.get("/v1/me", response_model=MeResponse)
    def v1_me(ctx=Depends(require_user)) -> MeResponse:
        org = accounts.get_org_by_id(ctx.org_id)
        if org is None:
            raise HTTPException(status_code=500, detail="org not found")
        return MeResponse(user=ctx.user, org=org)

    @app.post("/v1/keys", response_model=CreateApiKeyResponse, status_code=201)
    def v1_create_key(ctx=Depends(require_user)) -> CreateApiKeyResponse:
        return accounts.create_api_key(ctx.org_id)

    @app.get("/v1/keys", response_model=list[ApiKeyPublic])
    def v1_list_keys(ctx=Depends(require_user)) -> list[ApiKeyPublic]:
        return accounts.list_api_keys(ctx.org_id)

    @app.delete("/v1/keys/{key_id}")
    def v1_revoke_key(key_id: str, ctx=Depends(require_user)) -> dict:
        revoked = accounts.revoke_api_key(ctx.org_id, key_id)
        return {"revoked": revoked}

    @app.post("/v1/provenance", status_code=201)
    def v1_add(record: ProvenanceRecord, org_id: str = Depends(require_api_key)) -> dict:
        # Never trust org_id from the request body — always the authenticated key's org.
        record.org_id = org_id
        store.add(record)
        return {"added": True, "records": store.count(org_id=org_id)}

    @app.post("/v1/provenance/bulk", status_code=201)
    def v1_add_bulk(
        records: list[ProvenanceRecord], org_id: str = Depends(require_api_key)
    ) -> dict:
        for r in records:
            r.org_id = org_id
        n = store.add_many(records)
        return {"added": n, "records": store.count(org_id=org_id)}

    @app.post("/v1/resolve", response_model=ResolveResponse)
    def v1_resolve(req: ResolveRequest, org_id: str = Depends(require_api_key)) -> ResolveResponse:
        resp = resolve_provenance(store, req, repo=settings.target_repo, org_id=org_id)
        store.add_incident(
            IncidentRecord(
                org_id=org_id,
                commit_sha=req.commit_sha,
                file_path=req.file_path,
                line=req.line,
                exc_type=req.exc_type,
                exc_message=req.exc_message,
                resolved=resp.resolved,
                decision_id=resp.record.decision_id if resp.record else None,
                blast_radius=req.blast_radius,
                crash_trace_id=req.crash_trace_id or None,
                crash_span_id=req.crash_span_id or None,
            )
        )
        return resp

    @app.get("/v1/dashboard")
    def v1_dashboard(ctx=Depends(require_user)) -> dict:
        decisions = store.all(org_id=ctx.org_id)
        incidents = store.list_incidents(org_id=ctx.org_id)
        return {
            "org_id": ctx.org_id,
            "decision_count": len(decisions),
            "decisions": decisions,
            "incident_count": len(incidents),
            "resolved_incident_count": sum(1 for i in incidents if i.resolved),
            "incidents": incidents,
        }

    @app.get("/v1/leaderboard", response_model=LeaderboardReport)
    def v1_leaderboard(ctx=Depends(require_user)) -> LeaderboardReport:
        # The aggregate lens: rank every AI tool/model this org has recorded by real
        # production crash rate. Pure read over the same provenance + incidents tables.
        return compute_leaderboard(store, org_id=ctx.org_id)

    @app.post("/v1/risk-gate", response_model=RiskGateResponse)
    def v1_risk_gate(req: RiskGateRequest, ctx=Depends(require_user)) -> RiskGateResponse:
        # Prognosis without a git repo: score a pasted snippet against this org's history.
        return score_snippet(store, req.code, req.reasoning, org_id=ctx.org_id)

    @app.delete("/v1/provenance/{decision_id}")
    def v1_delete(decision_id: str, ctx=Depends(require_user)) -> dict:
        deleted = store.delete(decision_id, org_id=ctx.org_id)
        return {"deleted": deleted, "records": store.count(org_id=ctx.org_id)}

    return app


app = create_app()


def run() -> None:
    """Entry point: `codeautopsy-provenance`. Binds host/port from CODEAUTOPSY_PROVENANCE_URL."""
    import uvicorn

    settings = get_settings()
    parsed = urlparse(settings.provenance_url)
    uvicorn.run(app, host=parsed.hostname or "127.0.0.1", port=parsed.port or 8100)


if __name__ == "__main__":
    run()
