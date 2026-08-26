"""Application configuration.

Every setting comes from the environment. Nothing is read from a config file at
runtime, and no default ever contains a secret. The process either receives a
valid environment or refuses to start -- a bad config should fail at boot, in
front of a deploy pipeline, not on the first request in front of a user.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, immutable, environment-sourced configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        # An unrecognised APP_* variable is almost always a typo in a deploy
        # manifest. Rejecting it turns a silently-ignored setting into a
        # startup failure, which is the cheaper place to find out.
        extra="forbid",
        # Configuration does not change while the process is running.
        frozen=True,
    )

    # --- identity ---------------------------------------------------------
    service_name: str = "agent-harness"
    version: str = "0.1.0"
    environment: Literal["local", "dev", "staging", "prod"] = "local"

    # --- server -----------------------------------------------------------
    host: str = "0.0.0.0"  # binding all interfaces is correct inside a container
    port: int = Field(default=8000, ge=1, le=65535)

    # --- observability ----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # Tracing is off unless an endpoint is configured. A template that crashes
    # on a laptop because no collector is running would simply be deleted.
    otlp_endpoint: str | None = None
    trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    # --- behaviour --------------------------------------------------------
    request_timeout_seconds: float = Field(default=30.0, gt=0)

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"

    @property
    def tracing_enabled(self) -> bool:
        return self.otlp_endpoint is not None

    @model_validator(mode="after")
    def _enforce_environment_policy(self) -> Settings:
        """Config is a policy surface, not just a bag of values.

        Human-readable console logs are useful locally and useless to a log
        aggregator. Rather than trusting every deploy manifest to remember
        that, the constraint lives here where it cannot be forgotten.
        """
        if self.environment in ("staging", "prod") and self.log_format != "json":
            raise ValueError(
                f"log_format must be 'json' in {self.environment}, got '{self.log_format}'"
            )
        if self.environment == "prod" and self.log_level == "DEBUG":
            raise ValueError("log_level must not be DEBUG in prod")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings.

    Cached so the environment is parsed exactly once. Tests that need a
    different configuration should construct Settings() directly and pass it
    into create_app() rather than mutating this.
    """
    return Settings()
