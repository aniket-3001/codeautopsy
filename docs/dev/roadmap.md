# Roadmap — feature ideas borrowed from the competitor field

A competitive read of the other "Agents of SigNoz" hackathon entries surfaced six feature
ideas that map cleanly onto CodeAutopsy's existing architecture. This doc records the
ideas, where each one came from, and — since there isn't time to build all six before
submission — the ranking used to decide what actually gets built.

## The six ideas

| # | Idea | Borrowed from | What it'd add |
|---|---|---|---|
| 1 | **Dollar-cost accounting on Fix Bot + "savings from memory"** | BurnRate (tracks GenAI token cost via OTel) | Stamp each Fix Bot run's LLM cost on its record; when a lesson replays without hitting the LLM, compute and surface dollars saved. |
| 2 | **Self-instrumented MCP server, including silent-failure detection** | opentel-mcp (traces `CallToolResult.isError: true` inside otherwise-successful responses) | Wrap `codeautopsy-mcp`'s own tool calls (`autopsy`, `prognose`, `leaderboard`) in OTel spans — dogfooding: the tool other agents call is itself observed. |
| 3 | **Postmortem case-file generator** | Monitors in Black ("files a case" after every incident) | `codeautopsy report <trace>` renders the full chain of custody as a shareable markdown postmortem. |
| 4 | **Fix efficacy tracking (confirmed / refuted lessons)** | observable-agent (verifies a fix, rolls back if it doesn't hold) | If a crash fingerprint recurs after its fix PR merges, mark the lesson "refuted" and reopen it; otherwise mark "confirmed in prod." |
| 5 | **Chaos seeding command** | ChaosCart (deterministic multi-fault injection) | `codeautopsy chaos` seeds 3–4 distinct bug classes so the leaderboard/risk-gate/lessons memory are richly populated for a demo. |
| 6 | **CI-run linkage in the chain of custody** | GreenLight (AI session → commit → CI run → deploy → telemetry → recovery) | Stamp the GitHub Actions run URL and deploy revision alongside `DEPLOYED_COMMIT_SHA`, extending the chain one hop further: "…→ CI run → deployed revision." |

## Ranking, and why

Weighted by demo-impact-per-effort **and** a lesson learned the hard way this session: features
that require live SigNoz console/UI iteration to get right (dashboards, alert rules) are slow
and unreliable to build blind, without a way to test-verify locally. Pure-code features that
extend something already shipped and already tested are the safer bet with limited time left.

**Built:**

1. **#2 — MCP self-instrumentation.** ✅ Shipped. `codeautopsy/mcp/core.py`'s three tools
   (`autopsy`, `prognose`, `leaderboard`) are now each a span, with an injectable
   `tracer_provider` for tests (in-memory exporter, no console dependency) and a bootstrap in
   `server.py::run()` for the real stdio process. Includes an honest take on "silent failure"
   detection: exceptions flip the span to `ERROR` and re-raise; a legitimate negative result
   (e.g. `autopsy` finding no matching decision) is recorded as an attribute, *not* misreported
   as an error. Dogfooding: most competitors *consume* SigNoz's MCP server; CodeAutopsy is the
   one project that both emits its own MCP server *and* traces it.

2. **#6 — CI-run linkage.** ✅ Shipped. `CODEAUTOPSY_CI_RUN_URL` (`config.py`, set by
   `deploy-cloud-run.yml` from `github.server_url`/`github.repository`/`github.run_id`) is now
   stamped as `deployment.ci_run_url` on every `codeautopsy.autopsy` span, sent through
   `ResolveRequest`, persisted on `IncidentRecord` (both the SQLite and Postgres stores, with a
   migration for existing databases), and surfaced as a "CI run →" link in the dashboard's
   incident modal. Extends the chain of custody one step past GreenLight's own pitch — the
   single closest thesis-overlap competitor — with real data, no synthetic numbers.

**Reconsidered and dropped:**

- **#1 — dollar-cost accounting + "savings from memory."** Investigated, then deliberately
  **not built**. The part of the pitch that would've actually moved a judge — "the Fix Bot's
  memory saved you $X" — turned out to be false given the architecture: `run_fixbot`
  (`fixbot/core.py`) calls the LLM on *every* run, lesson or no lesson — `recall_lesson` only
  enriches the prompt, it never skips the call. `LessonRecord` stores a narrative sentence, not
  an executable patch, so there's nothing safe to replay without the LLM; building a real skip
  would mean bypassing the "propose a fresh patch, prove it with a regression test" step the
  whole governance story depends on — not worth the correctness risk this close to submission.
  The honest fallback version (real per-run cost tracking + "this bug class has cost $X across
  N recurrences," no "saved" framing) was assessed as a strictly weaker demo line than #6, with
  an added credibility risk of its own: Groq is free-tier, so any dollar figure is inherently
  synthetic ("if you were paying list price..."). Skipped in favor of #6.

**Deliberately not building, for now:**

- **#3 — postmortem generator.** Safe and cheap (all the data already exists), just less
  differentiating than 2/6. Worth doing if there's time left after #6.
- **#4 — fix efficacy tracking.** Good idea, real design complexity: what actually triggers
  "confirmed in prod" — a background check, a time window? Doesn't demo well live since it
  depends on time passing after a merge. Deprioritized given the clock.
- **#5 — chaos seeding.** Highest effort of the six, and the riskiest this close to
  submission — it touches the one seeded bug (`sample_app/main.py::parse_discount`) the
  entire live demo depends on. Also fairly redundant: the Live Autopsy playground already
  lets a judge exercise arbitrary risk patterns without needing multiple hardcoded bugs in
  the sample app.
