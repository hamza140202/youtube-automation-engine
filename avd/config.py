"""Configuration via env vars (pydantic-settings)."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config from env vars or .env file."""

    model_config = SettingsConfigDict(
        env_prefix="AVD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    download_dir: Path = Path("./download")
    db_path: Path = Path("~/.avd/jobs.sqlite").expanduser()
    logs_dir: Path = Path("./logs")

    # HTTP
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    proxy: str | None = None
    http_timeout_s: float = 30.0

    # Concurrency
    max_concurrency_global: int = 8
    max_concurrency_per_host: int = 3

    # Circuit breaker
    breaker_fail_max: int = 5
    breaker_reset_timeout_s: int = 300  # 5 min

    # Retry policy
    retry_max_attempts: int = 3
    retry_initial_wait_s: float = 0.6
    retry_max_wait_s: float = 10.0

    # Verifier
    min_size_bytes: int = 1024 * 1024  # 1 MB

    # Platform-specific
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_username: str | None = None
    reddit_password: str | None = None
    reddit_user_agent: str = "python:avd:1.0.0 (by /u/avd_bot)"

    douyin_dtk_url: str | None = None  # http://localhost:8000

    # Cobalt (optional self-hosted)
    cobalt_url: str | None = None

    # Log level
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton settings accessor."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
