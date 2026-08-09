"""Application settings, loaded from the environment / .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: List fields readable from the environment as comma-separated strings.
#: ``NoDecode`` stops pydantic-settings from JSON-parsing them before validation.
CSVList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core
    voicedesk_env: str = "development"
    api_port: int = 3010
    dashboard_port: int = 3011
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+asyncpg://voicedesk:voicedesk@localhost:5433/voicedesk"
    database_url_sync: str = "postgresql+psycopg://voicedesk:voicedesk@localhost:5433/voicedesk"
    db_echo: bool = False

    # Redis
    redis_url: str = "redis://localhost:6380/0"

    # Security
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    webhook_hmac_secret: str = "change-me-webhook-secret"
    recording_encryption_key: str = ""

    cors_origins: CSVList = Field(default_factory=lambda: ["http://localhost:3011"])
    rate_limit_per_minute: int = 100

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_models: CSVList = Field(
        default_factory=lambda: [
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemma-2-9b-it:free",
        ]
    )
    openrouter_timeout_seconds: float = 20.0
    openrouter_app_url: str = "https://voicedesk.apprend.tech"
    openrouter_app_name: str = "VoiceDesk"

    # Telephony
    telephony_provider: str = "mock"
    exotel_sid: str = ""
    exotel_api_key: str = ""
    exotel_api_token: str = ""
    exotel_subdomain: str = "api.exotel.com"
    knowlarity_api_key: str = ""
    knowlarity_sr_number: str = ""
    telephony_callback_base_url: str = "http://localhost:3010"

    # Object storage
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "voicedesk"
    s3_secret_key: str = "voicedesk123"
    s3_bucket: str = "voicedesk-recordings"
    s3_region: str = "ap-south-1"
    recording_retention_days: int = 90

    # WhatsApp
    whatsapp_provider: str = "mock"
    gosumo_api_url: str = "https://api.gosumo.io/v1"
    gosumo_api_key: str = ""
    whatsapp_handoff_confidence_threshold: float = 0.70

    # CRM handoff for qualified leads
    crm_push_enabled: bool = True
    #: A lead the CRM has rejected this many times is left alone; the failure
    #: is a misconfiguration to fix, not a delivery to keep retrying.
    crm_max_attempts: int = 10

    # TRAI compliance
    trai_calling_hour_start: int = 9
    trai_calling_hour_end: int = 21
    trai_timezone: str = "Asia/Kolkata"
    trai_dnd_check_enabled: bool = True
    trai_recording_consent_enabled: bool = True

    @field_validator("cors_origins", "openrouter_models", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow comma-separated strings in the environment for list fields."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.voicedesk_env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
