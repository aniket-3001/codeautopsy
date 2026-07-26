# CodeAutopsy semantic conventions (`codeautopsy.*`)

CodeAutopsy's whole thesis rides on two spans and one link between them. This is the mini
semantic-convention doc for the attributes it emits, so anyone querying the telemetry in SigNoz
(or building on it) knows exactly what each field means and where it comes from. Attribute names
follow OpenTelemetry style (lowercase, dot-namespaced); we reuse standard `code.*` and
`deployment.*` where they fit and add a `codeautopsy.*` namespace for what's ours.

## The two spans

| Span | `otel.library` | Emitted by | When |
|---|---|---|---|
| `agent.tool.<tool>` — the **decision** span | `codeautopsy.recorder` | recorder hook (`recorder/hooks.py`) | dev time, when an AI agent edits code |
| `codeautopsy.autopsy` — the **autopsy** span | `codeautopsy.enricher` | enricher (`enricher/core.py`) | run time, on an unhandled exception |

The autopsy span carries an OpenTelemetry **`Link`** to the decision span (`link.kind = decision`),
which is the join that lets `codeautopsy autopsy <trace>` walk from a crash back to the AI decision
that authored the crashing line.

That `Link` is a deliberate, explicit jump across two otherwise-unrelated traces — it's the core
thesis. It's distinct from **propagation**: `HTTPXClientInstrumentor` (both services, see
`docs/dev/operations.md`) makes the enricher's own HTTP call to the provenance service carry a
`traceparent` header, so *that* hop stays in the *same* trace as the crash span, the ordinary way
two services in one distributed trace do. Two different mechanisms for two different kinds of
connection: a Link jumps across dev-time/runtime; propagation keeps one causally-connected
runtime call in one trace.

---

## Decision span — `agent.tool.<tool>` (recorder)

Recorded when an AI coding agent writes code, one span per edit.

| Attribute | Type | Meaning |
|---|---|---|
| `agent.tool.name` | string | The coding tool that made the edit (e.g. `claude-code`, `cursor`). |
| `agent.session.id` | string | Session the edit belongs to — groups a burst of related edits. |
| `agent.decision.id` | string | Stable id for this decision; the join key into the provenance store. |
| `agent.reasoning` | string | The agent's stated reasoning for the edit (or `(no reasoning captured)`). |
| `code.file.path` | string | Repo-relative path of the edited file. |
| `code.lines.start` | int | First line of the edited range. |
| `code.lines.end` | int | Last line of the edited range. |
| `codeautopsy.risk_flags` | string | Comma-joined risk flags raised at write time (e.g. `unvalidated-input`). |
| `codeautopsy.risk_source` | string | Mandatory, closed to `heuristic` / `ai_judge` — which mechanism produced `risk_flags`. Always `heuristic` today (`recorder/risk.py` is pattern matching, no LLM judgment call); kept explicit so a future AI-judged signal can never silently blend with a deterministic one on a dashboard or in the leaderboard's crash-rate pricing. |

---

## Autopsy span — `codeautopsy.autopsy` (enricher)

Minted on an unhandled exception, linked back to the decision span.

| Attribute | Type | Meaning |
|---|---|---|
| `codeautopsy.resolved` | bool | Whether a decision was resolved for the crashing line. |
| `codeautopsy.cause_of_death` | string | Human-readable cause (e.g. `unhandled KeyError: 'code'`). |
| `codeautopsy.blast_radius` | int | How many other spans/requests the same crash touched. |
| `code.filepath` | string | Crashing file. |
| `code.lineno` | int | Crashing line. |
| `deployment.commit.sha` | string | The deployed commit the crash happened on. |
| `deployment.ci_run_url` | string | The GitHub Actions run that built + deployed the crashing revision. Only present when `CODEAUTOPSY_CI_RUN_URL` is configured — omitted entirely (not empty-string) outside a CI-deployed environment. |

When `codeautopsy.resolved = true`, the span also carries the resolved decision:

| Attribute | Type | Meaning |
|---|---|---|
| `codeautopsy.decision.id` | string | Id of the decision that authored the line (joins to the decision span). |
| `codeautopsy.decision.summary` | string | The decision's reasoning summary. |
| `codeautopsy.decision.trace_id` | string | Trace id of the decision span (for the link / a manual jump). |
| `codeautopsy.decision.span_id` | string | Span id of the decision span. |
| `codeautopsy.decision.session_id` | string | Session id of the authoring agent. |
| `codeautopsy.risk_flags` | string | Comma-joined risk flags recorded with the decision. |
| `codeautopsy.attribution.confidence` | double | 0–1 confidence that this decision authored the line. See [confidence](../../codeautopsy/provenance/confidence.py). |
| `codeautopsy.attribution.label` | string | Band for the score: `high` / `medium` / `low`. |
| `codeautopsy.attribution.match` | string | How it was matched: `exact-commit` or `git-blame`. |

---

## Autopsy log (enricher)

Alongside the span, `_emit_autopsy_log` (`codeautopsy/enricher/core.py`) emits one trace-correlated
log record per crash — same `trace_id`/`span_id` as the autopsy span, so it's queryable as a
SigNoz log line (e.g. full-text search over the reasoning), not just a span attribute you have to
open a trace to see.

| Field | Value | Meaning |
|---|---|---|
| `severity` | `INFO` / `WARN` | `INFO` when resolved, `WARN` when no decision matched. |
| `body` | string | Resolved: `"autopsy resolved: {cause_of_death} — {reasoning_summary}"`. Unresolved: `"autopsy unresolved: {cause_of_death}"`. |
| `codeautopsy.decision.id` | string | Only present when resolved. |
| `codeautopsy.decision.summary` | string | Only present when resolved — the AI's own reasoning. |
| `codeautopsy.risk_flags` | string | Only present when resolved. |
| `code.filepath` / `code.lineno` / `deployment.commit.sha` | — | Same crash coordinate as the span. |
| `codeautopsy.resolved` | bool | Always present, regardless of outcome. |

---

## MCP tool spans (`codeautopsy/mcp/core.py`)

Every call into `autopsy`/`prognose`/`postmortem`/`leaderboard`/`verify_provenance` — whether
from an MCP client (Cursor, Claude Desktop) or called directly — is its own span
(`codeautopsy.mcp.autopsy`, `.prognose`, `.postmortem`, `.leaderboard`, `.verify_provenance`). A
real exception flips the span `ERROR` and records it; a legitimate negative result (e.g.
`autopsy` finding no matching decision, or `verify_provenance` finding a broken chain) is
recorded as an attribute only — it is not misreported as an error.

| Span | Attribute | Meaning |
|---|---|---|
| `codeautopsy.mcp.autopsy` | `codeautopsy.mcp.resolved` | Whether a decision was found — present even when `false`. |
| `codeautopsy.mcp.prognose` | `codeautopsy.mcp.verdict` | `clear` / `flagged` / `priced`. |
| `codeautopsy.mcp.leaderboard` | `codeautopsy.mcp.total_decisions`, `codeautopsy.mcp.total_incidents` | Aggregate counts at call time. |
| `codeautopsy.mcp.verify_provenance` | `codeautopsy.mcp.chain_valid`, `codeautopsy.mcp.chain_length` | Recomputes the org's tamper-evidence hash chain (`provenance/integrity.py`) and compares it to what's stored; `chain_valid: false` means a record was altered after it was written. |

---

## The link

| Field | Value | Meaning |
|---|---|---|
| `codeautopsy.link.kind` | `decision` | Marks the autopsy→decision Link as the provenance join (vs any other link). |

---

## Git-trailer counterpart

The same provenance is also written into git history as commit trailers (so it survives a dropped
database). See [`provenance/trailers.py`](../../codeautopsy/provenance/trailers.py):
`Codeautopsy-Decision-Id`, `Codeautopsy-Traceparent` (a W3C traceparent to the decision span),
`Codeautopsy-Autopsy` (`file:line@commit`).
