"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime config for Choreizo.

    Values are read from environment variables (case-insensitive) with a `.env`
    file as a fallback. See `.env.example` for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required ---
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    admin_password: str = Field(default="", description="Bootstrap admin password")
    session_secret: str = Field(
        default="",
        description="Secret used to sign cookies and magic-link tokens",
    )
    base_url: str = Field(default="http://localhost:8000")

    # --- Timing ---
    house_timezone: str = "America/Los_Angeles"
    daily_assignment_hour: int = 6
    high_priority_reminder_interval_hours: int = 3
    escalation_after_hours: int = 24
    admin_notify_after_hours: int = 36
    rollover_high_priority: bool = True

    # --- Storage ---
    database_path: Path = Path("/data/choreizo.db")
    log_level: str = "INFO"

    # --- Telegram mode ---
    telegram_mode: str = "polling"  # or "webhook"

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # -- Derived URLs --

    @property
    def sqlite_async_url(self) -> str:
        """SQLAlchemy async URL used by the running application."""
        # Path("/data/choreizo.db") -> "sqlite+aiosqlite:////data/choreizo.db"
        return f"sqlite+aiosqlite:///{self.database_path}"

    @property
    def sqlite_sync_url(self) -> str:
        """Sync SQLAlchemy URL used by Alembic."""
        return f"sqlite:///{self.database_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
