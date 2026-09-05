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
    # "prod" is reachable: when APP_ENV == "prod" the demo/detection API-key
    # exemptions in app.main are disabled.
    APP_ENV: Literal["dev", "test", "demo", "prod"] = "dev"
    SIMULATION_MODE: bool = False

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_BASE_URL: str = "https://api.razorpay.com/v1"

    # LLM is optional. "none" (default) selects the offline heuristic reasoner.
    # Supported remote providers: "openai" and "pollinations" (both use the
    # same OpenAI-compatible chat contract and remain advisory only).
    LLM_PROVIDER: str = "none"
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    POLLINATIONS_API_KEY: str = ""
    POLLINATIONS_BASE_URL: str = "https://gen.pollinations.ai/v1"
    POLLINATIONS_MODEL: str = "openai"

    # Shared-secret for mutating /api/v1 routes (X-API-Key header).
    API_KEY: str = "dev-key"

    # Comma-separated list in env form; exposed as list[str].
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    POLICY_FILE: str = "policies/default.yaml"
    LOG_LEVEL: str = "INFO"

    # In-process worker tier (docs/worker.md). OFF by default: the test suite,
    # one-shot scripts, and the evaluation harness must never spawn the
    # background tick loop; production/demo deployments opt in explicitly.
    WORKER_ENABLED: bool = False
    WORKER_TICK_SECONDS: float = 30.0
    # Reconciliation sweep cadence (ADR 0011), run by the worker.
    WORKER_RECONCILE_SECONDS: float = 900.0
    # Detection pass cadence (real_test environment only), run by the worker.
    WORKER_DETECTION_SECONDS: float = 300.0
    # NotificationSender selection: "logging" (simulated default) |
    # "razorpay_notes" (real-environment seam — see app.services.worker.senders).
    WORKER_NOTIFICATION_SENDER: str = "logging"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
