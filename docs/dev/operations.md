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
| `HEAL_WEBHOOK_SECRET` | Shared secret guarding the Auto-Heal webhook (SigNoz → API) and the Fix Bot's report-back. Same value on the API **and** in GitHub Actions secrets. Has an insecure dev default — **must** be overridden in prod. |
| `CODEAUTOPSY_GITHUB_REPO` | `owner/name` the Fix Bot patches (e.g. `aniket-3001/codeautopsy`). Empty ⇒ dispatch is a graceful no-op. |
| `GITHUB_DISPATCH_TOKEN` | PAT (fine-grained, `contents:read`/`metadata` on that repo, or classic `repo`) allowed to fire the `autoheal` `repository_dispatch`. Empty ⇒ dispatch no-op. |
| `CODEAUTOPSY_PUBLIC_BASE_URL` | The API's own public URL, embedded in the dispatch so the workflow knows where to report the PR back. |

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
| GET | `/v1/leaderboard` | U | Rank the org's tools/models by real crash rate |
| POST | `/v1/risk-gate` | U | Price a pasted snippet against the org's history |
| POST | `/v1/heal/trigger` | U | Start a heal run in the caller's org (dashboard button) |
| POST | `/v1/heal/webhook` | S | SigNoz alert → start a `signoz-alert` heal run |
| GET | `/v1/heal/runs` | U | The org's heal runs + live timelines (polled by `#/autoheal`) |
| POST | `/v1/heal/{run_id}/complete` | S | Fix Bot reports the opened PR back |
| DELETE | `/v1/provenance/{decision_id}` | U | Remove a decision |

**S** = shared-secret (`X-Heal-Secret: $HEAL_WEBHOOK_SECRET`) — machine-to-machine, no user session.

> **Gotcha:** an empty-body `POST /v1/keys` via curl returns Cloud Run **411** unless you send
> `-H "Content-Length: 0"`. (httpx/browsers set it automatically.)

## CLI surface (`cli/main.py`)

```bash
codeautopsy autopsy <commit> <file> <line>     # resolve a crash → the AI decision (coroner report)
codeautopsy fix <commit> <file> <line> [--push] [--json] # Fix Bot: patch + regression test + commit/PR
                                                # --json: one machine-readable result line (Auto-Heal workflow)
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
| `prognosis.yml` | pull_request | Pre-mortem risk scan; comments the priced diff on the PR |
| `autoheal.yml` | `repository_dispatch` (`autoheal`) | Fix Bot patches the seeded bug, opens a PR, reports back to `#/autoheal` |

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

---

## Auto-Heal loop — one-time wiring

The loop is: **sample app crashes → SigNoz alert → API webhook → `repository_dispatch` → Fix Bot
opens a PR → reports back to `#/autoheal`.** Everything is code except two things a human has to
provision once. Until they're set, the loop still works end-to-end via the dashboard's **Trigger
Auto-Heal** button and degrades honestly (a missing GitHub token just marks the run
`dispatch_failed` with a truthful timeline).

**1. Secrets (three).** Same `HEAL_WEBHOOK_SECRET` on the API **and** in the GitHub repo's Actions
secrets, so the webhook and the Fix Bot's callback authenticate against each other.

| Where | Secret | Purpose |
|---|---|---|
| API env (Secret Manager) | `HEAL_WEBHOOK_SECRET` | authenticates SigNoz→API webhook + Fix Bot→API callback |
| API env | `GITHUB_DISPATCH_TOKEN`, `CODEAUTOPSY_GITHUB_REPO`, `CODEAUTOPSY_PUBLIC_BASE_URL` | fire `repository_dispatch`; tell the workflow where to report back |
| GitHub Actions secrets | `HEAL_WEBHOOK_SECRET`, `GROQ_API_KEY` | authenticate the callback; run the Fix Bot LLM |

**2. SigNoz alert + webhook channel.** In the SigNoz Cloud console:

- **Alert rule** on the metric `codeautopsy.crashes` (emitted by the sample app every 10s — see
  `otel.build_meter_provider`): threshold e.g. *sum over 1m > 0*, so a real crash trips it within
  seconds.
- **Notification channel** of type *Webhook* → `https://<CODEAUTOPSY_PUBLIC_BASE_URL>/v1/heal/webhook`,
  with header `X-Heal-Secret: <HEAL_WEBHOOK_SECRET>` and a JSON body of at least
  `{"org_id": "demo-public", "alert": "{{.CommonLabels.alertname}}"}`. Coordinates omitted ⇒ the
  Fix Bot targets the seeded bug (`codeautopsy/sample_app/main.py:91`).

The `GITHUB_DISPATCH_TOKEN` needs permission to POST `repository_dispatch` on
`CODEAUTOPSY_GITHUB_REPO` (fine-grained: *Contents: read* + *Metadata*, or a classic `repo` PAT).
