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
- 🚧 Recorder (Claude Code hooks), Sample app + Autopsy enricher, Coroner CLI, Fix bot.

## Components

| Component | Path | Role |
|---|---|---|
| Recorder | `codeautopsy/recorder/` | Claude Code hooks → dev-time decision spans + risk flags |
| Provenance | `codeautopsy/provenance/` | SQLite store + git-blame indexer + `resolve` API |
| Sample app | `codeautopsy/sample_app/` | Instrumented FastAPI "patient" with a seeded bug |
| Enricher | `codeautopsy/enricher/` | On exception, mints the linked `codeautopsy.autopsy` span |
| Coroner CLI | `codeautopsy/cli/` | `codeautopsy autopsy <trace>` — the chain of custody |
| Fix bot | `codeautopsy/fixbot/` | Reads the autopsy, opens a fix PR (closes the loop) |

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

## License

MIT
