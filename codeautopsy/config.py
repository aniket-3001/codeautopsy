"""Central configuration, loaded from environment / .env.

Every component (recorder, provenance service, sample app, enricher, CLI) reads the
same settings object so the OTLP wiring and service names stay consistent.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (codeautopsy/config.py -> project root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration for all CodeAutopsy components."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- OpenTelemetry / SigNoz ---------------------------------------------------
    otel_endpoint: str = Field(
        default="http://localhost:4318",
        alias="OTEL_EXPORTER_OTLP_ENDPOINT",
        description="OTLP base endpoint. /v1/traces etc. is appended per-signal.",
    )
    signoz_ingestion_key: str | None = Field(
        default=None,
        alias="SIGNOZ_INGESTION_KEY",
        description="SigNoz Cloud ingestion key; sent as the signoz-ingestion-key header.",
    )

    # --- Service identity ---------------------------------------------------------
    dev_service_name: str = Field(default="claude-code", alias="CODEAUTOPSY_DEV_SERVICE")
    runtime_service_name: str = Field(default="checkout-api", alias="CODEAUTOPSY_RUNTIME_SERVICE")

    # --- Provenance ---------------------------------------------------------------
    provenance_db: Path = Field(
        default=PROJECT_ROOT / "provenance.db", alias="CODEAUTOPSY_PROVENANCE_DB"
    )
    provenance_url: str = Field(
        default="http://localhost:8100", alias="CODEAUTOPSY_PROVENANCE_URL"
    )

    # --- Repo under observation ---------------------------------------------------
    target_repo: Path = Field(default=PROJECT_ROOT, alias="CODEAUTOPSY_TARGET_REPO")

    @property
    def otel_headers(self) -> dict[str, str]:
        """Headers for OTLP export (SigNoz Cloud auth)."""
        return {"signoz-ingestion-key": self.signoz_ingestion_key} if self.signoz_ingestion_key else {}

    def traces_endpoint(self) -> str:
        return f"{self.otel_endpoint.rstrip('/')}/v1/traces"

    def logs_endpoint(self) -> str:
        return f"{self.otel_endpoint.rstrip('/')}/v1/logs"

    def metrics_endpoint(self) -> str:
        return f"{self.otel_endpoint.rstrip('/')}/v1/metrics"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
