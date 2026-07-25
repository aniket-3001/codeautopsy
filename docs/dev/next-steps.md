# Next steps / pending tasks

Working notes to resume from — last updated after commit `0233832` (main, pushed, CI green).
If you're picking this back up cold: read the "What's solid" section first so you don't
re-verify work that's already done, then work top-down through "Pending."

## Pending — needs your action (I can't do these myself)

1. **Verify the distributed trace in SigNoz console.** This is the one open item from the
   last thing we shipped. Search Traces for `87db351fbc5cece02545065ea05f09c0` (or just the
   most recent trace on `checkout-api`). Before the last commit, this would've shown as two
   *disconnected* traces (the crash span, and a separate unrelated trace for the provenance
   service's own `/resolve` handling). It should now show as **one continuous waterfall**:
   `parse_discount` → the HTTP call → `codeautopsy-provenance`'s `POST /resolve` span, all
   under the same trace ID. I verified the propagation mechanism itself works correctly (real
   local-HTTP-server test, not a fake), and the deploy succeeded — this is just the visual
   confirmation in the console that only you can do.

2. **Decide on 5 leftover screenshot files**, not part of the repo:
   `D:\Aniket\SigNoz Hackathon\image.png`, `image copy.png`, `image copy 2.png`,
   `image copy 3.png`, `image copy 4.png` — dashboard-debugging screenshots from earlier in
   the session. Harmless either way; just flagged so they don't sit around unnoticed.

## Deferred, deliberately — from `docs/dev/roadmap.md`

Full reasoning is in that file. Quick recap of what's *not* built and why:

- **Postmortem case-file generator** (`codeautopsy report <trace>` → shareable markdown
  postmortem). Safe, cheap, all the data already exists (confidence scoring, decision,
  reasoning, lesson). Lowest-risk thing left on the list if there's time — least
  differentiating of the remaining ideas, but a nice demoable artifact.
- **Fix efficacy tracking** (mark a lesson "confirmed in prod" / "refuted" after a fix
  merges). Real design complexity (what actually triggers the check — a background job? a
  time window?) and doesn't demo well live since it depends on time passing after a merge.
- **Chaos seeding** (`codeautopsy chaos` — multiple seeded bug classes). Highest effort,
  touches the one seeded bug (`sample_app/main.py::parse_discount`) the whole live demo
  depends on. Also fairly redundant — the Live Autopsy playground already demos arbitrary
  risk patterns without needing hardcoded bugs.
- **Dollar-cost accounting / "savings from memory."** Investigated and dropped outright, not
  just deprioritized — the part of the pitch that would've mattered ("saved $X by skipping
  the LLM") turned out to be false given the architecture (`run_fixbot` calls the LLM on
  *every* run, lesson or not). Don't resurrect this without re-reading that section of
  `roadmap.md` first.

## Optional / console-heavy — higher risk, lower priority

- **SLOs / error budgets in SigNoz Cloud** (e.g. "99% of `/checkout` succeeds", track burn
  rate). Pairs narratively well with CodeAutopsy's reliability-pricing angle, but it's
  entirely console-configured — same failure mode as the dashboard-building attempt below.
  Only attempt this if there's real time to spare, and expect to iterate live with the
  console open (same collaborative back-and-forth pattern that worked for verifying the
  metrics/logs), not blind.
- **A second SigNoz dashboard** (visualizing `codeautopsy.decisions.indexed` /
  `codeautopsy.incidents`) was attempted and explicitly abandoned — a hand-written dashboard
  JSON didn't render correctly (metrics-panel query schema differs from the trace-panel
  schema the existing working dashboard uses, and the panels kept showing `0`/`No Data`
  after several rounds of fixes). Decision was to drop it rather than keep burning time. Not
  a gap that costs anything — the underlying metrics are already confirmed live and
  queryable; only the pretty visualization is missing.

## What's solid — don't re-verify, just trust it

Everything below has been tested (unit tests + a live production round-trip, not just "code
looks right") and is pushed to `main`:

- **Frontend test coverage** — 30 Playwright tests against the real `docs/*.html` files.
- **SigNoz instrumentation** — traces, metrics (`codeautopsy.decisions.indexed`,
  `codeautopsy.incidents`, `codeautopsy.crashes`), trace-correlated logs, and now distributed
  tracing (this session) — all confirmed live in SigNoz Cloud with real data, not just passing
  tests. The provenance service went from zero telemetry to fully instrumented.
- **A real telemetry-leak bug, found and fixed** — local `pytest` runs were exporting fake
  test data to real SigNoz Cloud (`tests/conftest.py` now prevents this permanently).
- **MCP self-instrumentation** — `autopsy`/`prognose`/`leaderboard` are each a span, with
  honest silent-failure handling (real exceptions → error; a legitimate "not found" →
  attribute only, not a false alarm).
- **CI-run linkage** — `deployment.ci_run_url` on every crash span, persisted on incidents,
  verified against a hand-built pre-migration database schema on *both* SQLite and Postgres
  (not just fresh test DBs), and confirmed end-to-end against live production (signup → API
  key → index a decision → resolve → dashboard shows the CI run link).
- **A full line-by-line review** of everything above was done separately (see conversation —
  not captured in a file), including a direct `gcloud` inspection of both deployed services'
  env vars. No code bugs found; only 3 stale-documentation numbers, all fixed.

Current test count: 258 Python + 30 Playwright = 288, ≥95% coverage gate enforced in CI,
ruff + mypy clean.

## Reference

- Competitive positioning (impact / creativity / technical excellence / best use of SigNoz vs.
  the other 9 hackathon entries) was discussed in conversation but not written to a file —
  worth writing down if you want it preserved past this session.
- `docs/dev/roadmap.md` — the 6 competitor-derived feature ideas, ranking, and what shipped.
