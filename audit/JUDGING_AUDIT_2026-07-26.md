# Judging Audit — 2026-07-26

Base commit: `c61150e1966ea197420d3c3ece4c5f2c69f4b439` (2026-07-26 02:23:43 +0530), plus one
uncommitted working-tree change (tamper-evidence hash chain + mandatory risk-source labeling,
see §5) not yet pushed or deployed at the time of writing.

## Methodology

Borrowed from a competitor entry's ("GreenLight") self-audit practice: every finding below
cites a command actually run, an HTTP response actually seen, or a `file:line` — no claim is
taken on faith. Six personas score independently against the hackathon's **actual published
judging criteria** (Potential Impact, Creativity & Innovation, Technical Excellence, Best Use
of SigNoz, User Experience, Presentation Quality) rather than invented categories, so
disagreement between personas surfaces real tradeoffs instead of being smoothed over.

## §1 — Verified facts (commands run this session, real output)

| Check | Command | Result |
|---|---|---|
| Full test suite | `pytest -q` | **307 passed, 12 skipped** (skips are Postgres-only tests, correctly gated on `DATABASE_URL` not being set locally) |
| Coverage | `coverage run -m pytest -q && coverage report` | **99%** (`TOTAL 2182 25 99%`), gate is `fail_under = 95` |
| Lint | `ruff check codeautopsy/ tests/` | All checks passed |
| Types | `mypy codeautopsy/` | Success: no issues found in 48 source files |
| Sample app (Cloud Run) | `curl .../health` | `{"status":"ok","commit":"c61150e1966ea197420d3c3ece4c5f2c69f4b439"}` — **live, and matches the latest pushed commit** |
| Provenance service (Cloud Run) | `curl .../health` | `{"status":"ok","records":9,"db":"postgres"}` — live, Postgres-backed, has real data |
| Landing / demo / dashboard (GitHub Pages) | `curl -o /dev/null -w '%{http_code}'` × 3 | 200 / 200 / 200 |
| PyPI package | `pypi.org/pypi/codeautopsy/json` | `0.1.0` is published and installable |
| CLI, live | `codeautopsy --help` | 10 real commands: `autopsy`, `provenance`, `lessons`, `recall`, `report`, `fix`, `prognose`, `index-commit`, `record`, `status` |
| Test-telemetry isolation | `tests/conftest.py:22` | `OTEL_EXPORTER_OTLP_ENDPOINT` is force-set to `127.0.0.1:4318` before any test runs — confirmed by the connection-refused noise in every local `pytest` run (nothing is listening on localhost:4318), and independently confirmed by `docs/dev/next-steps.md`'s own changelog entry: *"A real telemetry-leak bug, found and fixed — local pytest runs were exporting fake test data to real SigNoz Cloud."* This closes a risk a competitor audit (Agent-K) flagged in the abstract — CodeAutopsy already fixed the concrete version of it in a prior session. |

## §2 — Six-persona review

### Architecture / Technical Excellence judge
**Verdict: strong, with one live gap.** The dual-store Protocol (SQLite/Postgres kept in
lockstep), additive-only migrations, and org-scoped tenant isolation are real, tested
invariants (`docs/dev/codebase-map.md`), not aspirational prose — verified by reading
`provenance/store.py` and `store_postgres.py` directly. 99% coverage and a clean mypy-strict
pass across 48 files is unusually high for a hackathon submission.
**Downgrade risk:** the live Cloud Run deployment (commit `c61150e`) predates this session's
schema changes (§5) — a judge testing the *deployed* service won't see `verify_provenance` or
`risk_source` until it's redeployed. The local dev experience and the live demo are currently
two different versions of the product. **Action: redeploy before any live demo.**

### Best Use of SigNoz judge
**Verdict: genuinely load-bearing, not a checkbox.** Span links crossing the build/run
boundary are the core mechanism, not a bolted-on trace; both services are traced with
distributed propagation; the Auto-Heal loop is triggered by a real SigNoz alert → webhook, not
a poller. Verified live: the provenance service's own `/health` shows real production data (9
records), meaning the SigNoz telemetry describing it is describing something real, not a
seeded demo fixture.
**Downgrade risk:** only one alert type (threshold, on `codeautopsy.crashes`) exists. A
competitor (AugmentLoop) ships anomaly-detection *and* log-based alerts alongside a threshold
rule — a slow crash-rate creep that never crosses CodeAutopsy's fixed threshold currently goes
undetected. Not fixed this session (scoped out as a bigger lift); recorded here rather than
silently left off the record.

### Creativity & Innovation judge
**Verdict: the core trick is sound and, as far as this audit could determine, not duplicated
elsewhere in the field** — resolving a crash to the *reasoning* that authored it (not just the
commit) via a real OTel span link crossing the dev-time/runtime boundary. This session added a
second, smaller piece of novelty: `verify_provenance`'s hash chain makes "chain of custody" a
falsifiable claim (a judge can ask "prove nothing was edited" and get a real recomputed answer)
rather than a metaphor borrowed from forensics branding.
**Downgrade risk:** none identified this session that wasn't already known — GreenLight targets
an adjacent, overlapping problem space (see prior competitive comparison in this project's
history). Not re-litigated here since it doesn't change with a code audit.

### Potential Impact judge
**Verdict: broad, credible audience** — any developer using an AI coding agent, not a narrow
ops niche. The loop actually closes (Auto-Heal proposes, verifies with a real regression test,
opens a PR — never auto-merges), which is the concrete "so what" this project's own design
notes require of every feature. No code-level finding changes this score; it's a product-shape
judgment, not something this audit can move.

### User Experience judge
**Verdict: functional, with rough edges found by directly exercising it.**
- `codeautopsy --help` lists 10 commands; `docs/dev/codebase-map.md` documented only 5 before
  this audit (missing `provenance`, `lessons`, `recall`, `report`, `prognose` entirely) — fixed
  this session (§5), but flags that the CLI surface has been growing faster than its own
  reference doc.
- `codeautopsy status` against a fresh local `provenance.db` reports `decisions indexed: 0`
  with no hint that this is expected (a fresh local DB) versus a possible misconfiguration — a
  first-time user has no way to tell those apart from the output alone. **Not fixed this
  session** (UX polish, out of scope for today's changes) — recorded as a real, if minor, rough
  edge rather than omitted.
- The web dashboard, sandbox demo, and landing page all returned live 200s during this audit —
  no broken judge-facing links found.

### Presentation Quality judge
**Verdict: README is strong but was quietly out of sync with the shipped product before this
session.** Found and fixed: the MCP section undercounted its own tool count in four separate
places (three tools claimed/shown, five actually exist — `postmortem` was fully missing from
three of them, `verify_provenance` didn't exist yet in any). Test-count and coverage badges
were stale (296 → confirmed 307 via a real run, not assumed). `docs/dev/operations.md` and
`docs/dev/codebase-map.md` had the same class of drift, fixed as part of this session rather
than left for a judge to notice first.
**Downgrade risk:** `docs/dev/roadmap.md` and `docs/dev/next-steps.md` still reference tool
counts from earlier points in the project's history (e.g. "three tools" in a roadmap entry
proposed before `postmortem`/`verify_provenance` existed) — **deliberately left unedited**,
since those are changelog/point-in-time records, not current-state claims; rewriting them to
match today would misrepresent what was actually true when they were written (the same
historical-integrity principle GreenLight's own versioned regression-policy table uses, and
which this project's own `docs/dev/roadmap.md` preface already implies by being framed as a
dated planning record). Flagging the distinction explicitly here so it isn't mistaken for an
oversight on a future pass.

## §3 — Punch list, ranked by judge impact

| # | Finding | Judge affected | Fixed this session? | Est. cost if not yet done |
|---|---|---|---|---|
| 1 | Live Cloud Run deployment is one commit behind local (missing hash chain + risk_source) | Architecture, SigNoz | No — flagged, not fixed | ~10 min: push + redeploy workflow |
| 2 | README/docs MCP tool count stale (3 claimed, 5 real) | Presentation | **Yes** | — |
| 3 | Test/coverage badges stale (296 vs. real 307) | Presentation | **Yes** | — |
| 4 | `codebase-map.md` CLI command list incomplete (5 of 10 listed) | Presentation, UX | **Yes** | — |
| 5 | No anomaly/log-based SigNoz alerts, only threshold | Best Use of SigNoz | No — scoped out, bigger lift | ~1-2 hrs |
| 6 | `codeautopsy status` doesn't distinguish "fresh DB" from "misconfigured" | UX | No — minor polish | ~15 min |

## §4 — What this audit did *not* cover

No live demo was run end-to-end against the deployed services during this audit (health checks
only, not a full crash → autopsy → fix cycle against production). No manual click-through of
`docs/app.html`'s authenticated flows (signup/login/dashboard) was performed — only that the
page itself loads (HTTP 200). Both would be worth doing once §3 item 1 is resolved (no point
demoing against a deployment that's already one commit stale).
