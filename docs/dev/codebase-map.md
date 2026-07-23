# Codebase Map & Invariants

The whole product is a lookup: `(org_id, commit_sha, file_path, line) → AI decision`. Everything
below serves that join. Keep it boring; keep it reliable.

## Package layout (`codeautopsy/`)

| Module | Role |
|---|---|
| `config.py` | Central `Settings` (pydantic-settings, reads env / `.env`). Every component reads the **same** settings object. Key fields: `otel_endpoint`, `signoz_ingestion_key`, `provenance_url`, `database_url`, `api_key` (org key for the Enricher), `provenance_db`/`accounts_db` (local SQLite), `jwt_secret`, `groq_api_key`. |
| `otel.py` | OTLP/SigNoz tracer setup + `force_utf8_stdout()` (Windows console is cp1252). |
| `recorder/` | Captures AI decisions. `hooks.py` = the **Claude Code PostToolUse hook** (`codeautopsy-hook` entry point). `commit_indexer.py` binds pending edit-time decisions to a commit (`index-commit`). `risk.py` = risk-flag heuristics. `pending.py` = staged decisions. |
| `provenance/` | The heart. `models.py` (`ProvenanceRecord`, `ResolveRequest`, `ResolveResponse`, `IncidentRecord`). `indexer.py` (`resolve()` — the blame join). `store.py` (SQLite + `ProvenanceStoreProtocol`). `store_postgres.py` (Postgres). `service.py` (**the FastAPI app** — all HTTP endpoints). |
| `accounts/` | Multi-tenant SaaS: `models.py`, `store.py`/`store_postgres.py` (users, orgs, memberships, API keys), `security.py` (argon2 hashing, JWT), `auth.py` (`make_require_user`, `make_require_api_key` FastAPI deps). |
| `enricher/` | Runtime half. `core.py` = `autopsy_exception()` (mints the linked autopsy span on a crash) + `resolve_decision()`. `incidents.py` = local incident log (`.codeautopsy/incidents.jsonl`) the Fix Bot reads. |
| `fixbot/` | `core.py` `run_fixbot()` — assembles genealogy, calls an LLM (Groq, free tier) via forced tool-use for a structured patch, **runs a regression test as a real subprocess before committing**, commits on a `codeautopsy/fix-<id>` branch. `--push` opens a PR via `gh`. |
| `cli/main.py` | The Coroner CLI (typer). Commands: `autopsy`, `fix`, `index-commit`, `record`, `status`. |
| `sample_app/main.py` | A deliberately-buggy checkout API used to demo a live crash. |

## Frontend (`docs/`)

- `index.html` — marketing landing page.
- `demo.html` — public scripted sandbox (calls the **unauthenticated** `/provenance` + `/resolve`
  on the `demo-public` tenant, directly from the browser).
- `app.html` — the **dashboard SPA**: single file, Tailwind CDN, hash-router, JWT in localStorage,
  charts as inline SVG/CSS (no external libs). Routes: `#/home #/login #/signup #/onboarding
  #/dashboard #/autopsy #/integrate #/settings`. `#/autopsy` is the in-browser Live Autopsy
  playground; `#/integrate` is the copy-paste "Integrate in 3 steps" page (fills snippets with the
  org's real API key). Constants at top: `API` (Cloud Run base), `SIGNOZ_CONSOLE`.

---

## Invariants — break these and something silently rots

### 1. Dual store, always in lockstep
`service.py::_make_store()` picks `PostgresProvenanceStore` when `DATABASE_URL` is set, else the
SQLite `ProvenanceStore`. Both satisfy `ProvenanceStoreProtocol` (in `store.py`). **Any new method
or column must land in all three:** `store.py`, `store_postgres.py`, and the Protocol. Same pattern
for `accounts/store.py` vs `store_postgres.py`.

### 2. Tenant scope comes from the principal, never the client
`org_id` is always taken from the authenticated JWT/API-key, and the server **overwrites** any
`org_id` in a request body. Every store query filters by `org_id`. This is the #1 security
invariant — there is an isolation test (`tests/test_v1_api.py`) proving two orgs can't see each
other's data. The public `/resolve` and `/provenance` (no auth) operate only on `demo-public`.

### 3. Schema migrations run on startup (additively)
Both stores do `CREATE TABLE IF NOT EXISTS` **plus** explicit `ALTER TABLE ... ADD COLUMN`
migrations in `_init_schema()`, because the live tables predate later columns and
`CREATE TABLE IF NOT EXISTS` alone won't add them.
- SQLite: wrap each `ADD COLUMN` in `try/except sqlite3.OperationalError` (no `IF NOT EXISTS`).
- Postgres: `ADD COLUMN IF NOT EXISTS`.
When you add a column, add the migration too, or Cloud Run's existing DB will 500 on the new query.

### 4. The `/v1` API is split by principal
- **API-key** (`X-Api-Key`, org from key): `/v1/provenance`, `/v1/provenance/bulk`, `/v1/resolve`.
- **JWT** (`require_user`): `/v1/dashboard`, `/v1/me`, `/v1/keys`, `/v1/auth/*`.
See `operations.md` for the full endpoint table.

### 5. The Enricher's auth path
`resolve_decision()` posts to the authenticated `/v1/resolve` with `X-Api-Key` **when
`settings.api_key` is set** (so a hosted user's crash scopes to their org and lands on their
dashboard), and falls back to the public `/resolve` otherwise. Don't remove the fallback — the
scripted demo and tests depend on it. The crash's own `(trace_id, span_id)` are minted from the
autopsy span *after* the resolve, so the persisted incident carries them for the SigNoz deep-link.

### 6. Content-anchored decision ids
`decision_id` is a content hash (survives reformatting/rebase where line numbers drift), not a
random id. `codeautopsy record` uses `sha1(file:lines:reasoning)`. Resolve is by `(commit, file,
line)` range with **last-writer-wins** on overlap.
