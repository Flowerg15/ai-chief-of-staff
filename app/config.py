from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    telegram_bot_token: str
    telegram_chat_id: int
    telegram_webhook_secret: str
    anthropic_api_key: str
    supabase_url: str
    supabase_service_key: str
    gmail_client_id: str
    gmail_client_secret: str
    gmail_user_email: str
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_url: str
    brief_time_morning: str = "07:30"
    brief_time_afternoon: str = "13:00"
    timezone: str = "America/New_York"
    debug: bool = False
    log_level: str = "INFO"

    @field_validator("public_url")
    @classmethod
    def url_must_be_https(cls, v: str) -> str:
        if not v.startswith("https://") and not v.startswith("http://localhost"):
            raise ValueError("PUBLIC_URL must start with https:// (or http://localhost for local dev)")
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
