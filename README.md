# CodeAutopsy

> **Observability stops at the deploy. CodeAutopsy doesn't.**
> Trace a production bug back across the build/run boundary to the exact AI-agent decision —
> the *reasoning step* — that caused it. Then hand the agent its own autopsy so it fixes itself.

`git blame` tells you *which commit* broke prod. CodeAutopsy tells you *which reasoning step of
which AI agent* broke prod — using OpenTelemetry span links and SigNoz's cross-signal
correlation to walk from **crash → cause of death → the AI's original decision** in one click.

Built for the **WeMakeDevs × SigNoz** hackathon (Track 3 · Agents of SigNoz).

**Live:** [landing page](https://aniket-3001.github.io/codeautopsy/) ·
[**try the sandbox demo**](https://aniket-3001.github.io/codeautopsy/demo.html) ·
[sample app](https://codeautopsy-sample-app-182653908302.us-central1.run.app/health) ·
[provenance API](https://codeautopsy-provenance-182653908302.us-central1.run.app/health)

> **Judges — SigNoz proof without a login:** the web app shows captured screenshots of the real
> trace + blast-radius dashboard in-app (SigNoz Cloud has no anonymous sharing). Want *live*
> access to the SigNoz workspace? Email
> [angadjeetsingh7370@gmail.com](mailto:angadjeetsingh7370@gmail.com?subject=CodeAutopsy%20%E2%80%94%20SigNoz%20viewer%20access)
> and I'll add you as a read-only viewer.

---

## The one trick

A runtime stack frame gives you `file:line`. `git blame` at the *deployed* commit gives you the
commit that introduced that line. A **provenance index** maps `(commit, file, line-range)` →
the AI decision span that wrote it — including the reasoning the agent gave at the time. An OTel
**span link** stitches the runtime error trace to that dev-time decision trace, so one click in
SigNoz crosses a boundary no other observability tool instruments: **dev-time → runtime.**

```
RUNTIME  (checkout-api)                    DEV-TIME  (claude-code)
POST /checkout ─► parse_discount (500)     agent.turn ─► agent.tool.Edit
                       │                          ▲   reasoning: "assuming input is valid"
                       └─► codeautopsy.autopsy ───┘   (OTel span link — THE JUMP)
```

## SigNoz feature coverage

Not just traces. Every service CodeAutopsy runs is instrumented, and each SigNoz signal type
does a distinct, load-bearing job in the product — not a checkbox integration:

| SigNoz capability | Where it's used | Why |
|---|---|---|
| **Traces + span links** | `codeautopsy/enricher/core.py` (`codeautopsy.autopsy` span) | The core thesis: an OTel span link jumps a runtime crash trace to the dev-time decision trace that authored the crashing line. Validated on real SigNoz Cloud infra (`scripts/day0_smoke.py`). Every such span also carries `deployment.ci_run_url` — the GitHub Actions run that built and deployed the crashing revision — extending the chain one hop past the commit: *reasoning → commit → CI run → deployed revision*. |
| **Custom metrics** | `codeautopsy.crashes` (`sample_app/main.py`), `codeautopsy.decisions.indexed` + `codeautopsy.incidents` (`provenance/service.py`) | `codeautopsy.crashes` is what the Auto-Heal alert rule watches. The two provenance-service counters make ingest volume and resolution outcome queryable independent of any single trace. |
| **Logs, trace-correlated** | `enricher/core.py::_emit_autopsy_log` | The AI's own reasoning is emitted as a log record carrying the *same* trace/span id as the autopsy span — so "why did this crash" is filterable as a SigNoz log line, not something you have to open a trace to read. |
| **Alerts → webhook** | Alert rule on `codeautopsy.crashes` → `POST /v1/heal/webhook` (see `docs/dev/operations.md`) | Closes the Auto-Heal loop (L4): a real SigNoz alert — not a poller — is what triggers the Fix Bot with zero human in the loop. |
| **Dashboards** | [`dashboards/codeautopsy-blast-radius.json`](dashboards/codeautopsy-blast-radius.json) — 8 panels across traces, spanning overview stats, time series, a risk-flag leaderboard, and a live unresolved-crash queue | One click from the web app (`Blast Radius in SigNoz 🔍`) into a dashboard built entirely from `codeautopsy.autopsy` span attributes. |
| **MCP — emitting, not just consuming** | `codeautopsy/mcp/server.py` | The inverse of "agent queries SigNoz's MCP server": CodeAutopsy *is* an MCP server, exposing `autopsy`/`prognose`/`leaderboard` as agent-callable tools over the provenance index SigNoz's own MCP server has no way to know about. |
| **Both services traced** | `sample_app/main.py` **and** `provenance/service.py` (`FastAPIInstrumentor`) | The dashboard/API backend isn't a silent, unobserved control plane — it's a first-class instrumented service in the same SigNoz Cloud tenant as the sample app. |

Every service exports to a **real, hosted SigNoz Cloud tenant** (`in2` region), not a local
ephemeral container spun up for the demo — the telemetry above is what's actually live in
production right now.

## Status

- ✅ **Day-0 validated:** span-link click navigates across traces/services in SigNoz Cloud
  (`scripts/day0_smoke.py`). The core thesis is proven on real infrastructure.
- ✅ Provenance store + git-blame join engine + resolve API (`codeautopsy/provenance/`).
- ✅ Recorder — real Claude Code `PostToolUse` hook (`codeautopsy-hook`, wired via
  `.claude/settings.json`), risk-flag detection, commit indexer.
- ✅ Sample app (checkout-api with a seeded bug) + Autopsy Enricher (mints the linked
  `codeautopsy.autopsy` span, plus a trace-correlated log of the AI's own reasoning) + incident
  log for reproduction context.
- ✅ Coroner CLI — `codeautopsy autopsy`, `index-commit`, `status`.
- ✅ Fix Bot — `codeautopsy fix <commit> <file> <line>`: feeds the agent its own genealogy,
  verifies the patch with a real regression test before committing anything, opens a PR via
  `gh` with `--push`.
- ✅ 270+ tests (246 Python + 30 Playwright frontend), ≥95% coverage gate enforced in CI
  (`ruff check` + `mypy` + `pytest` clean).
- ✅ Dockerized (`docker compose up`) and CI/CD via GitHub Actions — lint/type/test (backend
  *and* frontend) on every push, image published to GHCR on `main`, landing page deployed via
  GitHub Pages.
- ✅ Live on Google Cloud Run (see [Deployment](#deployment) below) — provenance + sample app,
  both instrumented (traces, metrics, and — for the sample app — trace-correlated logs).
- ✅ Persistent store — provenance data lives in Cloud SQL (Postgres) and survives redeploys
- ✅ Interactive [sandbox demo](https://aniket-3001.github.io/codeautopsy/demo.html) — trigger the real bug, submit a decision, watch it resolve, live
  deployed and validated end-to-end on real infra, redeployed automatically on every push to
  `main`.
- 🚧 Stretch: self-learning lesson write-back to the agent's rules file; a second SigNoz
  dashboard built on the new provenance-service metrics.

**Landing page:** https://aniket-3001.github.io/codeautopsy/ — built from
[`docs/index.html`](docs/index.html), deployed via GitHub Pages
(`.github/workflows/pages.yml`) on push to `main`.

## Components

| Component | Path | Role |
|---|---|---|
| Recorder | `codeautopsy/recorder/` | Claude Code hooks → dev-time decision spans + risk flags |
| Provenance | `codeautopsy/provenance/` | SQLite (default) or Postgres (`DATABASE_URL`) store + git-blame indexer + `resolve` API |
| Sample app | `codeautopsy/sample_app/` | Instrumented FastAPI "patient" with a seeded bug |
| Enricher | `codeautopsy/enricher/` | On exception, mints the linked `codeautopsy.autopsy` span |
| Coroner CLI | `codeautopsy/cli/` | `codeautopsy autopsy <trace>` — the chain of custody |
| Fix Bot | `codeautopsy/fixbot/` | `codeautopsy fix <trace>` — patch, verify, commit, PR |
| MCP server | `codeautopsy/mcp/` | `codeautopsy-mcp` — exposes `autopsy`/`prognose`/`leaderboard` as agent-callable tools |

**Docs:** [semantic conventions](docs/dev/semconv.md) (the `codeautopsy.*` span attributes) ·
[Fix Bot governance](docs/dev/governance.md) (autonomy levels — the loop stops at a PR) ·
[operations](docs/dev/operations.md) · [reproduce locally](docs/dev/reproduce.md).

## MCP server — CodeAutopsy as agent-callable tools

Most Agents-of-SigNoz projects *consume* SigNoz's MCP server so their agent can read telemetry.
CodeAutopsy points the plug the other way: it **is** an MCP server, exposing the one thing only
CodeAutopsy knows — the map from a crashing line back to the AI decision that authored it — so any
MCP client (Cursor, Claude Desktop, an IDE) gets three tools on its menu:

| Tool | Question it answers |
|---|---|
| `autopsy(commit, file, line)` | Which AI coding decision authored this crashing line? |
| `prognose(code)` | What's this snippet's risk, priced against real crash history? |
| `leaderboard()` | Which AI tools/models crash most in production? |

```bash
pip install -e ".[mcp]"    # brings in the `mcp` package
codeautopsy-mcp            # runs the server over stdio (the transport clients launch)
```

Register it with a client by pointing a stdio server at the `codeautopsy-mcp` command:

```jsonc
// e.g. Claude Desktop / Cursor mcp config
{
  "mcpServers": {
    "codeautopsy": { "command": "codeautopsy-mcp" }
  }
}
```

The server reads the developer's **own** local provenance index (SQLite, or Postgres when
`DATABASE_URL` is set) — no network hop. See `docs/dev/operations.md` for details.

## Quickstart

```bash
python -m pip install -e ".[dev]"
cp .env.example .env          # add your SigNoz Cloud endpoint + ingestion key
pytest                        # provenance join engine is fully unit-tested
python scripts/day0_smoke.py  # emit the two linked traces into SigNoz
```

## Configuration

All config comes from environment / `.env` (see `.env.example`). Key vars:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — SigNoz OTLP endpoint (e.g. `https://ingest.in2.signoz.cloud:443`)
- `SIGNOZ_INGESTION_KEY` — SigNoz Cloud ingestion key (git-ignored; never commit)
- `GROQ_API_KEY` — required only for the Fix Bot (`codeautopsy fix`); free key at https://console.groq.com/keys

## Docker

Run the whole spine (provenance service + instrumented sample app) without a local Python
install:

```bash
docker compose up --build
```

This starts `provenance` (port `8100`) and `sample-app` (port `8000`), sharing a network and a
named volume for `provenance.db`. `sample-app` waits for `provenance`'s healthcheck before
starting. Both containers use an **editable** install (`pip install -e`) so `.git` history ships
inside the image and `git blame`-based resolution behaves identically to a bare-metal checkout —
`sample_app`'s `REPO_ROOT` depends on this. Override `OTEL_EXPORTER_OTLP_ENDPOINT` and
`SIGNOZ_INGESTION_KEY` via a `.env` file to point the containers at SigNoz Cloud.

**Run the whole demo locally, no Cloud, no login wall.** Our hosted demo uses SigNoz Cloud
(no anonymous sharing). To reproduce end-to-end against a **self-hosted SigNoz you control** —
either one command via Foundry ([`casting.yaml`](casting.yaml)) or your own SigNoz + our compose —
see **[docs/dev/reproduce.md](docs/dev/reproduce.md)**.

```bash
curl http://localhost:8000/health                                           # {"status":"ok","commit":"<sha>"}
curl -X POST http://localhost:8000/checkout -d '{"discount_code":"10","subtotal":100}'
```

## CI/CD

GitHub Actions (`.github/workflows/`):

- **`ci.yml`** — on every push/PR to `main`: editable install, `ruff check`, `mypy`, `pytest`
  with coverage (`fail_under = 95`, see `pyproject.toml`), coverage XML uploaded as an artifact.
  Runs a `postgres:16` service container so `tests/test_provenance_postgres.py` exercises the
  real Postgres backend (skipped locally when `DATABASE_URL` isn't set). A second `frontend` job
  runs the Playwright suite (see [Frontend tests](#frontend-tests) below) against the real
  `docs/*.html` files with the backend mocked.
- **`docker-publish.yml`** — on push to `main` (or manual dispatch): builds the image and
  publishes it to GHCR (`ghcr.io/<owner>/<repo>`), tagged by commit SHA and `latest`.
- **`pages.yml`** — on push to `main` touching `docs/`: deploys `docs/index.html` to GitHub
  Pages.
- **`deploy-cloud-run.yml`** — on push to `main` (or manual dispatch): builds the image, pushes
  it to Artifact Registry, and redeploys both Cloud Run services. Authenticates via Workload
  Identity Federation (no long-lived key stored in GitHub).

## Frontend tests

`docs/` is deliberately build-step-free — static HTML with inline vanilla JS, deployed as-is to
GitHub Pages. Test coverage (`e2e/`) uses [Playwright](https://playwright.dev) to drive the real
`docs/app.html`, `docs/demo.html`, and `docs/index.html` files unmodified in a real browser, with
`fetch()` calls to the Cloud Run API intercepted and mocked (`e2e/fixtures.js`) — no live backend
needed. It covers the router's auth-gated redirects, the auth/signup/login/logout flow, dashboard
rendering + filtering + incident modal, the Live Autopsy playground, Risk Gate, Prognosis, the
Leaderboard, Settings/API keys, the Integrate snippets, and the sandbox demo's 3-step crash loop —
plus a regression test that the Auto-Heal poll loop actually stops when you navigate away from
`#/autoheal` (`stopHealPoll()`).

```bash
npm install
npx playwright install --with-deps chromium   # first run only
npm run test:e2e                              # headless
npm run test:e2e:ui                           # interactive UI mode
```

Runs on every push/PR via the `frontend` job in `ci.yml`. Nothing under `e2e/`,
`package.json`, or `playwright.config.js` is published — GitHub Pages only ships `docs/`.

## Deployment

Live on Google Cloud Run, project `codeautopsy-hackathon`, region `us-central1`:

- **Provenance**: https://codeautopsy-provenance-182653908302.us-central1.run.app
  (`min-instances=1` so the `resolve` API stays warm for a demo)
- **Sample app**: https://codeautopsy-sample-app-182653908302.us-central1.run.app
  (points its `CODEAUTOPSY_PROVENANCE_URL` at the provenance service above)

```bash
curl https://codeautopsy-sample-app-182653908302.us-central1.run.app/health
```

**Persistence:** the provenance service is backed by Cloud SQL (Postgres, instance
`codeautopsy-db`), connected via the Cloud SQL Auth Proxy socket (`--add-cloudsql-instances`)
with the DSN injected from Secret Manager (`--set-secrets=DATABASE_URL=...`) — never as a
plaintext env var. Data survives redeploys; verified by submitting a record, forcing a fresh
Cloud Run revision, and confirming it's still there. Local dev and the test suite still default
to the zero-config SQLite store (`ProvenanceStore` in `codeautopsy/provenance/store.py`) unless
`DATABASE_URL` is set.

To reproduce the deploy manually (e.g. onto a different GCP project):

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
docker build -t us-central1-docker.pkg.dev/<project>/codeautopsy/app:latest .
docker push us-central1-docker.pkg.dev/<project>/codeautopsy/app:latest

gcloud run deploy codeautopsy-provenance --image=us-central1-docker.pkg.dev/<project>/codeautopsy/app:latest \
  --command=codeautopsy-provenance --port=8100 --min-instances=1 --max-instances=1 \
  --set-env-vars="CODEAUTOPSY_PROVENANCE_URL=http://0.0.0.0:8100,CODEAUTOPSY_TARGET_REPO=/app" \
  --allow-unauthenticated

gcloud run deploy codeautopsy-sample-app --image=us-central1-docker.pkg.dev/<project>/codeautopsy/app:latest \
  --command=codeautopsy-sample --port=8000 \
  --set-env-vars="CODEAUTOPSY_PROVENANCE_URL=<provenance-url-from-above>,CODEAUTOPSY_TARGET_REPO=/app,CODEAUTOPSY_RUNTIME_SERVICE=checkout-api" \
  --allow-unauthenticated
```

## License

MIT
