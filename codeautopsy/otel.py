"""Shared OpenTelemetry bootstrap.

One place that knows how to build a tracer/logger provider pointed at SigNoz (Cloud or
self-hosted). Components call `build_tracer_provider(...)` with their own service name so
each shows up correctly in SigNoz's service map.
"""

from __future__ import annotations

import sys

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from codeautopsy.config import Settings, get_settings


def force_utf8_stdout() -> None:
    """Windows consoles default to cp1252 and choke on unicode; force UTF-8 output."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _resource(service_name: str, service_version: str, extra: dict | None = None) -> Resource:
    attrs = {
        "service.name": service_name,
        "service.version": service_version,
    }
    if extra:
        attrs.update(extra)
    return Resource.create(attrs)


def build_tracer_provider(
    service_name: str,
    service_version: str = "0.1.0",
    resource_attrs: dict | None = None,
    settings: Settings | None = None,
) -> TracerProvider:
    """A TracerProvider that exports over OTLP/HTTP to SigNoz, with Cloud auth headers."""
    settings = settings or get_settings()
    provider = TracerProvider(resource=_resource(service_name, service_version, resource_attrs))
    exporter = OTLPSpanExporter(
        endpoint=settings.traces_endpoint(),
        headers=settings.otel_headers or None,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def build_logger_provider(
    service_name: str,
    service_version: str = "0.1.0",
    resource_attrs: dict | None = None,
    settings: Settings | None = None,
) -> LoggerProvider:
    """A LoggerProvider so agent reasoning transcripts land as trace-correlated logs."""
    settings = settings or get_settings()
    provider = LoggerProvider(resource=_resource(service_name, service_version, resource_attrs))
    exporter = OTLPLogExporter(
        endpoint=settings.logs_endpoint(),
        headers=settings.otel_headers or None,
    )
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return provider


def build_meter_provider(
    service_name: str,
    service_version: str = "0.1.0",
    resource_attrs: dict | None = None,
    settings: Settings | None = None,
    export_interval_ms: int = 10000,
) -> MeterProvider:
    """A MeterProvider so the sample app can emit a crash counter SigNoz can alert on.

    This is the metric that closes the Auto-Heal loop: a real OTel Counter exported to
    SigNoz, where an alert rule watches its rate and fires the heal webhook. Short export
    interval so a crash storm shows up in SigNoz (and trips the alert) within seconds, not
    minutes — the demo can't wait for a default 60s push.
    """
    settings = settings or get_settings()
    exporter = OTLPMetricExporter(
        endpoint=settings.metrics_endpoint(),
        headers=settings.otel_headers or None,
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=export_interval_ms)
    return MeterProvider(
        resource=_resource(service_name, service_version, resource_attrs),
        metric_readers=[reader],
    )
