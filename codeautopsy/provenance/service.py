"""FastAPI provenance service — the `resolve` endpoint the Autopsy Enricher calls.

On a runtime exception, the enricher POSTs (commit, file, line) here and gets back the AI
decision that authored that line, which it then attaches to the autopsy span as a link.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import FastAPI

from codeautopsy.config import Settings, get_settings
from codeautopsy.provenance.indexer import resolve as resolve_provenance
from codeautopsy.provenance.models import ProvenanceRecord, ResolveRequest, ResolveResponse
from codeautopsy.provenance.store import ProvenanceStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = ProvenanceStore(settings.provenance_db)
    app = FastAPI(title="CodeAutopsy Provenance", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "records": store.count(), "db": str(settings.provenance_db)}

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

    @app.post("/resolve", response_model=ResolveResponse)
    def resolve(req: ResolveRequest) -> ResolveResponse:
        return resolve_provenance(store, req, repo=settings.target_repo)

    return app


app = create_app()


def run() -> None:
    """Entry point: `codeautopsy-provenance`. Binds to the host/port in CODEAUTOPSY_PROVENANCE_URL."""
    import uvicorn

    settings = get_settings()
    parsed = urlparse(settings.provenance_url)
    uvicorn.run(app, host=parsed.hostname or "127.0.0.1", port=parsed.port or 8100)


if __name__ == "__main__":
    run()
