from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_webhook_secret: str

    # Anthropic
    anthropic_api_key: str

    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Gmail
    gmail_client_id: str
    gmail_client_secret: str
    gmail_user_email: str

    # Server
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_url: str  # Must be HTTPS for Telegram webhooks

    # Scheduling
    brief_time_morning: str = "07:30"
    brief_time_afternoon: str = "13:00"
    timezone: str = "America/New_York"

    # App
    debug: bool = False
    log_level: str = "INFO"

    @field_validator("public_url")
    @classmethod
    def url_must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("PUBLIC_URL must start with https://")
        return v.rstrip("/")

    @property
    def webhook_url(self) -> str:
        return f"{self.public_url}/telegram/webhook"

    @property
    def gmail_oauth_redirect_uri(self) -> str:
        return f"{self.public_url}/gmail/oauth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
