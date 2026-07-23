"""FastAPI provenance service — the `resolve` endpoint the Autopsy Enricher calls.

On a runtime exception, the enricher POSTs (commit, file, line) here and gets back the AI
decision that authored that line, which it then attaches to the autopsy span as a link.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from codeautopsy.config import Settings, get_settings
from codeautopsy.provenance.indexer import resolve as resolve_provenance
from codeautopsy.provenance.models import ProvenanceRecord, ResolveRequest, ResolveResponse
from codeautopsy.provenance.store import ProvenanceStore, ProvenanceStoreProtocol

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = _make_store(settings)
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
