"""Service settings, read once from the environment (and `.env` locally)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_NAME = "ai-twin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- service ---------------------------------------------------------
    app_env: Literal["local", "test", "dev", "prod"] = "local"
    # `vault` holds every tier and the admin routes; `public` serves published chunks only.
    deployment_profile: Literal["vault", "public"] = "vault"
    service_version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console", "auto"] = "auto"

    # --- dependencies ------------------------------------------------------
    mongodb_uri: str = "mongodb://localhost:27018"
    mongodb_database: str = "ai_twin"
    redis_url: str = "redis://localhost:6380/0"
    qdrant_url: str = "http://localhost:6334"
    # Each readiness check gets this long before it counts as unavailable.
    health_check_timeout_seconds: float = Field(default=1.0, gt=0, le=10)

    # --- feature flags -------------------------------------------------------
    # Hosted classifier for non-sensitive decisions; capped at the public tier.
    ff_jev_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
