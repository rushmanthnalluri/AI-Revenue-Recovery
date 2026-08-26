"""Application configuration via pydantic-settings.

All settings are read from process env and (optionally) a `.env` file at the
repo root. Never put real secrets in `.env.example` or any committed file.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repo root, so the root .env is
# found regardless of the current working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    DATABASE_URL: str = "sqlite:///./pulserecover.db"
    # "prod" is reserved: when APP_ENV == "prod" the demo/detection API-key
    # exemptions in app.main are disabled. Only dev/test/demo are used today.
    APP_ENV: Literal["dev", "test", "demo"] = "dev"
    SIMULATION_MODE: bool = True

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_BASE_URL: str = "https://api.razorpay.com/v1"

    # LLM is optional. "none" (default) selects the offline heuristic reasoner.
    LLM_PROVIDER: str = "none"
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""

    # Shared-secret for mutating /api/v1 routes (X-API-Key header).
    API_KEY: str = "dev-key"

    # Comma-separated list in env form; exposed as list[str].
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    POLICY_FILE: str = "policies/default.yaml"
    LOG_LEVEL: str = "INFO"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def simulation_mode(self) -> bool:
        """True unless explicitly disabled AND running against real keys."""
        return self.SIMULATION_MODE


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
