# CodeAutopsy

> **Observability stops at the deploy. CodeAutopsy doesn't.**
> Trace a production bug back across the build/run boundary to the exact AI-agent decision —
> the *reasoning step* — that caused it. Then hand the agent its own autopsy so it fixes itself.

`git blame` tells you *which commit* broke prod. CodeAutopsy tells you *which reasoning step of
which AI agent* broke prod — using OpenTelemetry span links and SigNoz's cross-signal
correlation to walk from **crash → cause of death → the AI's original decision** in one click.

Built for the **WeMakeDevs × SigNoz** hackathon (Track 3 · Agents of SigNoz).

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

## Status

- ✅ **Day-0 validated:** span-link click navigates across traces/services in SigNoz Cloud
  (`scripts/day0_smoke.py`). The core thesis is proven on real infrastructure.
- ✅ Provenance store + git-blame join engine + resolve API (`codeautopsy/provenance/`).
- ✅ Recorder — real Claude Code `PostToolUse` hook (`codeautopsy-hook`, wired via
  `.claude/settings.json`), risk-flag detection, commit indexer.
- ✅ Sample app (checkout-api with a seeded bug) + Autopsy Enricher (mints the linked
  `codeautopsy.autopsy` span) + incident log for reproduction context.
- ✅ Coroner CLI — `codeautopsy autopsy`, `index-commit`, `status`.
- ✅ Fix Bot — `codeautopsy fix <commit> <file> <line>`: feeds the agent its own genealogy,
  verifies the patch with a real regression test before committing anything, opens a PR via
  `gh` with `--push`. 101 tests passing, 100% coverage, ruff + mypy clean (`pytest`).
- ✅ Dockerized (`docker compose up`) and CI/CD via GitHub Actions — lint/type/test on every
  push, image published to GHCR on `main`, landing page deployed via GitHub Pages.
- 🚧 Stretch: fully-automatic loop via SigNoz alert webhook; self-learning lesson write-back
  to the agent's rules file; SigNoz dashboards.

**Landing page:** [`docs/index.html`](docs/index.html) — deployed via GitHub Pages
(`.github/workflows/pages.yml`) on push to `main`.

## Components

| Component | Path | Role |
|---|---|---|
| Recorder | `codeautopsy/recorder/` | Claude Code hooks → dev-time decision spans + risk flags |
| Provenance | `codeautopsy/provenance/` | SQLite store + git-blame indexer + `resolve` API |
| Sample app | `codeautopsy/sample_app/` | Instrumented FastAPI "patient" with a seeded bug |
| Enricher | `codeautopsy/enricher/` | On exception, mints the linked `codeautopsy.autopsy` span |
| Coroner CLI | `codeautopsy/cli/` | `codeautopsy autopsy <trace>` — the chain of custody |
| Fix Bot | `codeautopsy/fixbot/` | `codeautopsy fix <trace>` — patch, verify, commit, PR |

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

```bash
curl http://localhost:8000/health                                           # {"status":"ok","commit":"<sha>"}
curl -X POST http://localhost:8000/checkout -d '{"discount_code":"10","subtotal":100}'
```

## CI/CD

GitHub Actions (`.github/workflows/`):

- **`ci.yml`** — on every push/PR to `main`: editable install, `ruff check`, `mypy`, `pytest`
  with coverage (`fail_under = 95`, see `pyproject.toml`), coverage XML uploaded as an artifact.
- **`docker-publish.yml`** — on push to `main` (or manual dispatch): builds the image and
  publishes it to GHCR (`ghcr.io/<owner>/<repo>`), tagged by commit SHA and `latest`.
- **`pages.yml`** — on push to `main` touching `docs/`: deploys `docs/index.html` to GitHub
  Pages.

## License

MIT
