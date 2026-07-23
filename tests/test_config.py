"""Tests for Settings (endpoint URL building, SigNoz Cloud auth headers) and the shared
OTel bootstrap (`codeautopsy/otel.py`) — previously untested in isolation.
"""

from __future__ import annotations

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider

from codeautopsy.config import Settings, get_settings
from codeautopsy.otel import build_logger_provider, build_tracer_provider, force_utf8_stdout


def test_endpoint_builders_append_signal_path():
    settings = Settings(OTEL_EXPORTER_OTLP_ENDPOINT="https://ingest.example.cloud:443")
    assert settings.traces_endpoint() == "https://ingest.example.cloud:443/v1/traces"
    assert settings.logs_endpoint() == "https://ingest.example.cloud:443/v1/logs"
    assert settings.metrics_endpoint() == "https://ingest.example.cloud:443/v1/metrics"


def test_endpoint_builders_strip_trailing_slash():
    settings = Settings(OTEL_EXPORTER_OTLP_ENDPOINT="https://ingest.example.cloud:443/")
    assert settings.traces_endpoint() == "https://ingest.example.cloud:443/v1/traces"


def test_otel_headers_empty_without_ingestion_key():
    settings = Settings(SIGNOZ_INGESTION_KEY=None)
    assert settings.otel_headers == {}


def test_otel_headers_carries_signoz_ingestion_key():
    settings = Settings(SIGNOZ_INGESTION_KEY="secret-key")
    assert settings.otel_headers == {"signoz-ingestion-key": "secret-key"}


def test_build_tracer_provider_returns_configured_provider():
    settings = Settings(OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318")
    provider = build_tracer_provider("test-service", settings=settings)
    assert isinstance(provider, TracerProvider)


def test_build_logger_provider_returns_configured_provider():
    settings = Settings(OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318")
    provider = build_logger_provider("test-service", resource_attrs={"extra": "attr"}, settings=settings)
    assert isinstance(provider, LoggerProvider)


def test_force_utf8_stdout_reconfigures_streams_that_support_it(monkeypatch):
    calls = []

    class _FakeStream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("codeautopsy.otel.sys.stdout", _FakeStream())
    monkeypatch.setattr("codeautopsy.otel.sys.stderr", _FakeStream())
    force_utf8_stdout()
    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 2


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
