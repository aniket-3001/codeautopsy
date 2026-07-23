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
    database_url: str | None = Field(
        default=None,
        alias="DATABASE_URL",
        description="Postgres DSN. When set, the provenance service persists here instead of "
        "the local SQLite file — set in production so data survives Cloud Run redeploys.",
    )
    api_key: str | None = Field(
        default=None,
        alias="CODEAUTOPSY_API_KEY",
        description="Hosted org API key. When set, the enricher resolves crashes against the "
        "authenticated /v1/resolve — so they scope to your org and appear on your dashboard.",
    )

    # --- Repo under observation ---------------------------------------------------
    target_repo: Path = Field(default=PROJECT_ROOT, alias="CODEAUTOPSY_TARGET_REPO")

    # --- Accounts / multi-tenant SaaS (M1) ------------------------------------------
    accounts_db: Path = Field(
        default=PROJECT_ROOT / "accounts.db", alias="CODEAUTOPSY_ACCOUNTS_DB"
    )
    jwt_secret: str = Field(
        default="dev-only-insecure-secret-change-me",
        alias="JWT_SECRET",
        description="HS256 signing secret for dashboard session tokens. Must be overridden "
        "(via Secret Manager) in any deployed environment.",
    )
    jwt_expires_seconds: int = Field(default=86400, alias="JWT_EXPIRES_SECONDS")

    # --- Fix Bot --------------------------------------------------------------------
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    fixbot_model: str = Field(default="llama-3.3-70b-versatile", alias="CODEAUTOPSY_FIXBOT_MODEL")

    @property
    def otel_headers(self) -> dict[str, str]:
        """Headers for OTLP export (SigNoz Cloud auth)."""
        if self.signoz_ingestion_key:
            return {"signoz-ingestion-key": self.signoz_ingestion_key}
        return {}

    def traces_endpoint(self) -> str:
        return f"{self.otel_endpoint.rstrip('/')}/v1/traces"

    def logs_endpoint(self) -> str:
        return f"{self.otel_endpoint.rstrip('/')}/v1/logs"

    def metrics_endpoint(self) -> str:
        return f"{self.otel_endpoint.rstrip('/')}/v1/metrics"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
