# Reproduce CodeAutopsy locally — no Cloud, no login wall

Our hosted demo runs against **SigNoz Cloud**, which has no anonymous sharing — an external link
dead-ends a judge at a login screen. So the whole demo is also runnable **entirely on your own
machine**, against a **self-hosted SigNoz you control**. You see the real linked traces in your
own SigNoz UI; you depend on nothing of ours.

There are two ways to do it. Pick one.

---

## Option A — one command, via Foundry (`casting.yaml`)

[`casting.yaml`](../../casting.yaml) declares the whole stack for Foundry: a self-hosted SigNoz
**and** CodeAutopsy's own services (`provenance`, `sample-app`), wired so our traces export into
that local SigNoz.

```bash
curl -fsSL https://signoz.io/foundry.sh | bash    # installs foundryctl
foundryctl cast -f casting.yaml                    # forges + deploys everything
```

[`casting.yaml.lock`](../../casting.yaml.lock) is committed alongside it — `forge` writes it from
checksums of the rendered deployment, pinning the exact resolved config so a re-`cast` reproduces
the same deployment rather than whatever `casting.yaml`'s templates happen to resolve to later.

This brings up:

| Service | Where |
|---|---|
| SigNoz UI | http://localhost:8080 |
| SigNoz OTLP ingest | `:4317` (gRPC) / `:4318` (HTTP) |
| `signoz-mcp` (SigNoz's MCP) | `:8000` |
| CodeAutopsy provenance API | http://localhost:8100 |
| sample-app (the "patient") | reachable at http://localhost:8000 |

> On first run SigNoz asks **you** to create a local admin account — it's your instance on your
> machine, so there are no credentials of ours to share. That is the whole point: reproducibility
> without our Cloud tenant.

Now generate the two linked traces and watch them land in *your* SigNoz:

```bash
curl -X POST http://localhost:8000/checkout -d '{"discount_code":"AB","subtotal":100}'
```

In the SigNoz UI, open the `sample-app` trace for that request: the crash span carries a
`codeautopsy.autopsy` child linked back to the AI **decision** span that authored the crashing
line. That span link is the thesis, and here it is in a backend you own.

---

## Option B — self-host SigNoz yourself, then run our compose against it

If you already run SigNoz (or prefer to bring it up the official way):

```bash
# 1. Self-host SigNoz (its own docker-compose) — OTLP ends up on host :4317/:4318, UI on :8080/:3301
git clone -b main https://github.com/SigNoz/signoz && cd signoz/deploy/docker
docker compose up -d
cd -

# 2. Run CodeAutopsy's stack, exporting into that local SigNoz.
#    host.docker.internal resolves to the host from inside our containers (Linux included —
#    docker-compose.yml maps it to host-gateway).
OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318 \
SIGNOZ_INGESTION_KEY= \
docker compose up --build

# 3. Trigger the crash and inspect the linked traces in your SigNoz.
curl -X POST http://localhost:8000/checkout -d '{"discount_code":"AB","subtotal":100}'
```

Leaving `SIGNOZ_INGESTION_KEY` empty is correct for self-hosted SigNoz — the ingestion key is a
SigNoz **Cloud** concept.

---

## What you should see either way

* A `sample-app` trace whose exception span has a linked `codeautopsy.autopsy` span.
* That autopsy span's attributes: `codeautopsy.decision.id`, `codeautopsy.decision.summary`,
  `codeautopsy.attribution.confidence` / `.label` / `.match`, `codeautopsy.blast_radius`.
* A `Link` from the autopsy span to the original AI **decision** span — the join that lets
  `codeautopsy autopsy <trace>` walk from the crash back to the decision that caused it.

No part of this needs our Cloud account.
