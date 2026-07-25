# Alerts — wiring SigNoz to CodeAutopsy's Auto-Heal loop

These alert rules watch CodeAutopsy's own custom metrics (`codeautopsy.crashes`,
`codeautopsy.incidents`, `codeautopsy.decisions.indexed` — see `codeautopsy/sample_app/main.py`
and `codeautopsy/provenance/service.py`). Rule 1 is not just monitoring: it is the missing piece
that turns the Auto-Heal loop from "an endpoint that exists" into "an endpoint SigNoz actually
calls." The code already expects it — see the comment above `crash_counter` in
`sample_app/main.py` and above `v1_heal_webhook` in `provenance/service.py` — this folder is
what makes that comment true.

## 1. Create the webhook notification channel (for rule 1 — the loop-closer)

In SigNoz -> **Settings -> Notification Channels -> New -> Webhook**:

- **Name:** `codeautopsy-autoheal-webhook`
- **Webhook URL:** `https://codeautopsy-provenance-182653908302.us-central1.run.app/v1/heal/webhook`
- **Method:** `POST`
- **Custom header:** `X-Heal-Secret: <the value of HEAL_WEBHOOK_SECRET on the provenance
  Cloud Run service>` — this is the same shared secret `_require_heal_secret` in
  `provenance/service.py` checks with a constant-time compare. Without it the webhook gets a
  401.
- **Body:** the default SigNoz alert payload is fine as-is. `HealWebhookRequest` (in
  `codeautopsy/autoheal/models.py`) only reads a few optional fields out of SigNoz's rich alert
  body and falls back to the sample app's seeded bug coordinates for anything it doesn't find —
  it was written to tolerate whatever SigNoz actually sends, not a hand-crafted payload.

## 2. Create the alert rules

Each rule in [`alert-rules.json`](alert-rules.json) targets a metric that's already being
exported — no new instrumentation needed, just query + threshold:

| Alert | Metric | Fires when | Wired to |
|---|---|---|---|
| checkout-api crash detected | `codeautopsy.crashes` | any crash in 1 min | `codeautopsy-autoheal-webhook` -> `/v1/heal/webhook` -> Fix Bot |
| provenance service incident volume | `codeautopsy.incidents` | > 3 resolutions in 5 min | informational (pick any channel) |
| decision-recording pipeline flatline | `codeautopsy.decisions.indexed` | 0 decisions in 30 min | informational (pick any channel) |

In SigNoz -> **Alerts -> New Alert**, rebuild the query shown in the JSON in the query builder
(exact filter-picker UI varies a little by SigNoz version — treat the JSON as the metric +
threshold to recreate, not a guaranteed blind import), then attach the channel from the table
above.

The threshold on rule 1 is deliberately ">0 crashes in 1 minute," not a percentage-based rate:
this is a low-traffic hackathon demo app, not production traffic, so a single crash — a judge
clicking the crash button once — *is* the event worth firing on, not noise to average out.

## 3. See the loop run

```bash
curl -X POST https://codeautopsy-sample-app-182653908302.us-central1.run.app/checkout \
  -H "Content-Type: application/json" -d '{"discount_code": "GIMME50"}'
```

Within the 1-minute eval window: SigNoz's `codeautopsy.crashes` counter trips the rule, the
webhook hits `/v1/heal/webhook` with `X-Heal-Secret`, `create_heal_run` starts a heal run
(`trigger="signoz-alert"`, visible on the dashboard's Auto-Heal tab), and — same as the manual
"Trigger heal" button — a `repository_dispatch` fires `autoheal.yml`, which runs the Fix Bot and
opens a PR carrying the chain of custody. The whole chain (crash -> alert -> webhook -> heal run
-> GitHub Actions -> PR) is one correlated story across SigNoz and GitHub, exactly like the
`codeautopsy-blast-radius` dashboard (`../dashboards/`) shows for the manual path.
