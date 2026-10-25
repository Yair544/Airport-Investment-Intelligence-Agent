"""Application configuration loaded from environment variables and an optional `.env` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All runtime limits and provider settings live here so they can be tuned without code changes."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment and serving
    debug: bool = False
    auto_bootstrap: bool = False
    data_dir: Path = REPO_ROOT / "data"
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:8000"]
    )
    cookie_secure: bool = False
    session_cookie_name: str = "aiia_session"
    session_ttl_hours: float = 12.0

    # OpenAI (provider-specific calls stay in app/agent/client.py)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-6-luna"
    openai_reasoning_effort: str = "low"
    openai_timeout_seconds: float = 60.0
    openai_max_retries: int = 2
    openai_max_output_tokens: int = 8000

    # Agent loop bounds
    agent_max_model_rounds: int = 6
    agent_max_tool_calls: int = 10
    chat_request_budget_seconds: float = 120.0
    chat_lease_margin_seconds: float = 30.0
    chat_max_attempts: int = 3
    history_max_messages: int = 12
    history_max_chars: int = 16000
    tool_output_max_chars: int = 24000

    # Request limits
    max_body_bytes: int = 64 * 1024
    max_message_chars: int = 4000
    max_active_requests_per_user: int = 2
    max_active_requests_total: int = 16
    rate_limit_chat_per_minute: int = 20
    rate_limit_login_per_minute: int = 10
    rate_limit_register_per_hour: int = 5
    rate_limit_status_per_minute: int = 120
    rate_limit_refresh_per_hour: int = 6

    # Data refresh and downloads
    refresh_budget_seconds: float = 3 * 3600.0
    refresh_lease_seconds: float = 300.0
    download_timeout_seconds: float = 180.0
    download_max_retries: int = 6
    download_max_bytes: int = 400 * 1024 * 1024
    data_history_years: int = 2

    # Analysis defaults (see docs/architecture.md section 4)
    forecast_horizon_years: int = 5
    primary_airport_min_enplanements: int = 10_000
    s3_min_departures: int = 1_000
    long_haul_threshold_miles: float = 3452.0

    @property
    def secure_cookies(self) -> bool:
        """Secure cookies when configured, or whenever the app is served from an https origin."""
        return self.cookie_secure or any(origin.startswith("https://") for origin in self.allowed_origins)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def raw_data_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def lease_seconds(self) -> float:
        return self.chat_request_budget_seconds + self.chat_lease_margin_seconds


@lru_cache
def get_settings() -> Settings:
    return Settings()
