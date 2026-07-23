# Operations — Run, Test, Deploy, Release

All commands assume repo root `codeautopsy/`. Windows shell is PowerShell; a Bash tool is also
available. The console is cp1252 — scripts call `force_utf8_stdout()`.

## Local setup

```bash
pip install -e ".[dev]"          # editable install + dev tools (pytest, ruff, mypy)
cp .env.example .env             # then fill in SigNoz key etc. (git-ignored — never commit)
```

Local runs default to **SQLite** (`provenance.db`, `accounts.db`). Set `DATABASE_URL` to use
Postgres instead.

## Run

```bash
codeautopsy-provenance           # the FastAPI backend (binds from CODEAUTOPSY_PROVENANCE_URL)
codeautopsy-sample               # the deliberately-buggy sample checkout app
codeautopsy status               # provenance store + config summary
```

## Test / lint / type-check (the gate)

```bash
pytest -q                        # 134 passing, 10 skipped (skips need a live Postgres)
ruff check codeautopsy/          # lint
mypy codeautopsy/                # types
```

To exercise the Postgres store locally, run a `postgres:16` container and set `DATABASE_URL`; CI
does this via a service container (see `.github/workflows/ci.yml`).

---

## Environment variables (`config.py`)

| Env var | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP base (SigNoz). Prod: `https://ingest.in2.signoz.cloud:443`. **Default is `localhost:4318`** — if unset in a deploy, exports silently no-op. |
| `SIGNOZ_INGESTION_KEY` | Sent as `signoz-ingestion-key` header. In git-ignored `.env` locally; Secret Manager in prod. |
| `CODEAUTOPSY_PROVENANCE_URL` | Where clients/CLI/Enricher reach the backend. |
| `DATABASE_URL` | Postgres DSN. When set, backend uses Postgres; else SQLite. |
| `CODEAUTOPSY_API_KEY` | Org API key for the **Enricher** → resolve via authenticated `/v1/resolve`. |
| `JWT_SECRET` | HS256 signing secret for dashboard sessions. **Must** be overridden in prod (Secret Manager). |
| `GROQ_API_KEY` | Fix Bot LLM (free Groq, `llama-3.3-70b-versatile`). |

---

## HTTP API surface (`provenance/service.py`)

Auth: **U** = user JWT (`Authorization: Bearer`), **K** = API key (`X-Api-Key`), **—** = public.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | — | Liveness + record count + db kind |
| POST | `/provenance`, `/provenance/bulk` | — | Public demo ingest (`demo-public`) |
| GET | `/provenance` | — | Public list |
| POST | `/resolve` | — | Public resolve (`demo-public`) |
| POST | `/v1/auth/signup`, `/v1/auth/login` | — | Create user+org / login → JWT |
| GET | `/v1/me` | U | Current user + org |
| POST/GET/DELETE | `/v1/keys`, `/v1/keys/{id}` | U | Mint / list (prefix only) / revoke API keys |
| POST | `/v1/provenance`, `/v1/provenance/bulk` | K | Tenant-scoped decision ingest |
| POST | `/v1/resolve` | K | Tenant-scoped resolve; **persists an incident** |
| GET | `/v1/dashboard` | U | Org's decisions + incidents + stats |
| DELETE | `/v1/provenance/{decision_id}` | U | Remove a decision |

> **Gotcha:** an empty-body `POST /v1/keys` via curl returns Cloud Run **411** unless you send
> `-H "Content-Length: 0"`. (httpx/browsers set it automatically.)

## CLI surface (`cli/main.py`)

```bash
codeautopsy autopsy <commit> <file> <line>     # resolve a crash → the AI decision (coroner report)
codeautopsy fix <commit> <file> <line> [--push] # Fix Bot: patch + regression test + commit/PR
codeautopsy index-commit [--repo PATH]         # bind pending edit-time decisions to HEAD
codeautopsy record --commit .. --file .. --lines 40-46 \
    --reasoning ".." --risk-flag .. --tool cursor [--api-key K --api-url URL]
codeautopsy status
```

`record` is the **agent-agnostic** path (any tool/script/human). With `--api-key` it POSTs to the
hosted `/v1/provenance`; without, it writes to the local SQLite store.

---

## Deploy

Push to `main` triggers GitHub Actions (`.github/workflows/`):

| Workflow | Trigger | Effect |
|---|---|---|
| `deploy-cloud-run.yml` | push to `main` | Deploys the backend to Cloud Run; **runs schema migrations** on startup |
| `pages.yml` | push touching `docs/**` | Deploys `docs/` to GitHub Pages |
| `ci.yml` | push / PR | Test suite (with a Postgres service container) |
| `docker-publish.yml` | push to `main` | Builds + publishes the Docker image |
| `publish-pypi.yml` | push tag `v*.*.*` | Builds + publishes to PyPI (see below) |

GCP project `codeautopsy-hackathon`: two Cloud Run services (`codeautopsy-provenance`,
`codeautopsy-sample-app`), Cloud SQL Postgres (`codeautopsy-db`), Artifact Registry, Secret Manager,
keyless deploy via Workload Identity Federation.

### Cloud Run + OTel gotcha (cost us a debugging session)
Cloud Run only guarantees CPU **while a request is in flight**. A `BatchSpanProcessor` exports on a
background thread that can be frozen after the response returns — so `autopsy_exception()` calls
`provider.force_flush(timeout_millis=3000)` inside the request window before returning. Also verify
OTLP endpoint/auth env vars are wired into the **specific** deploy step (each service is configured
independently).

---

## Cut a PyPI release

Publishing uses **Trusted Publishing (OIDC)** — no token is stored anywhere. The PyPI "pending
publisher" is already configured (project `codeautopsy`, owner `aniket-3001`, repo `codeautopsy`,
workflow `publish-pypi.yml`, environment `pypi`).

```bash
# 1. bump the version in pyproject.toml, e.g. 0.1.0 -> 0.1.1
# 2. commit it, then tag + push:
git tag -a v0.1.1 -m "codeautopsy 0.1.1"
git push origin v0.1.1
# 3. publish-pypi.yml builds sdist+wheel, runs `twine check`, publishes via OIDC.
```

- Published versions are **immutable** — a mistake means bumping the version, not re-uploading.
- Validate locally first: `python -m build && python -m twine check dist/*`.
- Install story for users (both work): `pip install codeautopsy` or
  `pip install git+https://github.com/aniket-3001/codeautopsy.git`.
