# CodeAutopsy — Developer & Agent Guide

Orientation for anyone (human or a future Claude session) picking up this repo. This folder
(`docs/dev/`) documents **what actually exists and how to work on it**. For the product *vision
and milestone plan* (some of which is intentionally roadmap-only), see the root
[`ARCHITECTURE.md`](../../ARCHITECTURE.md).

> `docs/` is also the **GitHub Pages site** (`index.html`, `app.html`, `demo.html`). These `.md`
> files live under `docs/dev/` so they don't clutter the site root. Editing anything under `docs/`
> triggers the "Deploy Landing Page" workflow — harmless, it just redeploys.

---

## What CodeAutopsy is (one paragraph)

Observability stops at the deploy; CodeAutopsy doesn't. It traces a **production crash back across
the build/run boundary to the exact AI-agent decision that authored the crashing line** — the
reasoning and risk flags the agent had when it wrote that code — via an OpenTelemetry **span link**,
and can close the loop with an auto-fix PR (Fix Bot). It is a **multi-tenant hosted SaaS** (accounts,
API keys, per-org isolation, a dashboard) plus a `pip install`-able package (Recorder + Enricher +
CLI). Built for the **WeMakeDevs × SigNoz hackathon, Track 3 (Agents of SigNoz)** — a demo that
*feels* like a real product a stranger can sign up for, not a commercial operation (no billing).

## The mental model (two sentences)

1. **Record**: when an AI agent writes code, its reasoning is captured as a **decision** indexed
   against `(org, commit, file, line-range)`.
2. **Resolve**: when the deployed app crashes at `(commit, file, line)`, a keyed lookup (a "git
   blame" join) returns the decision that authored that line, and the Enricher mints a SigNoz span
   link from the crash span to the original decision span.

Everything else (dashboard, incidents timeline, Fix Bot, Integrate page) is built around that join.

---

## Current status (2026-07)

- **Live and working**: multi-tenant accounts + API keys + JWT dashboard; tenant-scoped
  ingestion/resolve; incidents timeline; the span-link "money shot" validated in SigNoz Cloud;
  published to **PyPI as `codeautopsy` 0.1.0**; agent-agnostic `codeautopsy record`; the Enricher
  resolves against the authenticated `/v1/resolve` when an org key is set.
- **Deployed**: backend on Cloud Run (Postgres via Cloud SQL), dashboard + landing on GitHub Pages.
- **Roadmap-only (intentionally not built)**: GitHub OAuth / GitHub App (M4), Postgres RLS / audit
  logs / rate limiting (M7). See `ARCHITECTURE.md` §9.
- **Tests**: 134 passing, 10 skipped (skips need a live Postgres). `ruff` + `mypy` clean.

## Live URLs

| What | URL |
|---|---|
| Provenance/API backend (Cloud Run) | `https://codeautopsy-provenance-3bczbiamba-uc.a.run.app` |
| Landing page (Pages) | `https://aniket-3001.github.io/codeautopsy/` |
| Dashboard SPA | `https://aniket-3001.github.io/codeautopsy/app.html` |
| Scripted sandbox demo | `https://aniket-3001.github.io/codeautopsy/demo.html` |
| SigNoz Cloud console (India `in2`) | `https://mighty-bonefish.in2.signoz.cloud` |
| GitHub repo | `https://github.com/aniket-3001/codeautopsy` |
| PyPI | `https://pypi.org/project/codeautopsy/` |

## Where to look next

- **[`codebase-map.md`](codebase-map.md)** — module-by-module map, key files, and the invariants you
  must not break (dual store, tenant scoping, schema migrations).
- **[`operations.md`](operations.md)** — how to run, test, deploy, and cut a PyPI release; env vars;
  the HTTP API + CLI surface; and the platform gotchas (Windows cp1252, Cloud Run `force_flush`,
  empty-POST `Content-Length`).

## House rules that have bitten us before

- **Never commit without being asked.** Deploys happen on push to `main` (see `operations.md`).
- **Secrets never enter the repo.** Local secrets live in git-ignored `.env`; production secrets in
  GCP Secret Manager. Use the session scratchpad for temp files, not the repo.
- **Two stores, always in sync.** Any provenance/incident capability must be added to *both*
  `ProvenanceStore` (SQLite) and `PostgresProvenanceStore`, plus the `ProvenanceStoreProtocol`.
- **Be honest about built vs. roadmap.** Don't overclaim; synthetic trace ids resolve to a "no data"
  screen in SigNoz — only real instrumented crashes produce resolvable traces.
