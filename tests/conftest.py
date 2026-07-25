"""Test-session safety net: never let telemetry from a test run reach real SigNoz Cloud.

`sample_app/main.py` and `provenance/service.py` bootstrap real, *process-global*
TracerProvider/MeterProvider/LoggerProvider at **import time** via `get_settings()`
(`codeautopsy.config.Settings` reads a developer's local `.env`, which — for anyone who's
followed `docs/dev/operations.md` and pointed it at real SigNoz Cloud for `day0_smoke.py`
— has live ingestion credentials). Once `test_sample_app.py` (or anything importing those
modules) triggers that bootstrap, it becomes the global OTel state for the *entire* pytest
process — including every other test that calls `autopsy_exception()` without explicitly
passing its own in-memory `tracer_provider`/`logger_provider` override.

A plain fixture runs too late: module-level code executes at import/collection time, before
any fixture body does. `conftest.py` is imported by pytest before it collects any test
module, so setting these here — as plain environment variables, which pydantic-settings
prioritizes over the `.env` file — guarantees every provider built anywhere during the test
session points at a harmless local endpoint that no-ops instead of shipping test fixtures
(crash counters, fake org_ids, "WeirdError: something odd") to production.
"""

import os

os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:4318"
os.environ["SIGNOZ_INGESTION_KEY"] = ""
