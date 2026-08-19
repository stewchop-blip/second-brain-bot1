"""Configuration management for Second Brain Bot."""
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Config:
    # Required (no defaults — must come first in dataclass)
    bot_token: str
    database_url: str
    openrouter_api_key: str

    # Telegram
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_path: str = "/webhook"
    port: int = 8080
    bot_username: str | None = None

    # LLM (OpenRouter)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model: str = "gpt-4o-mini"
    max_tokens: int = 1500
    temperature: float = 0.7
    request_timeout: int = 30
    max_retries: int = 3

    # Limits
    daily_limit: int = 20
    max_message_length: int = 4000
    history_limit: int = 10  # messages to keep for context

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        # Railway provides DATABASE_URL automatically; fall back to common names
        database_url = (
            os.getenv("DATABASE_URL")
            or os.getenv("DATABASE_PRIVATE_URL")
            or os.getenv("POSTGRES_URL")
            or os.getenv("POSTGRES_PRIVATE_URL")
        )
        if not database_url:
            raise RuntimeError("Missing required environment variable: DATABASE_URL (or DATABASE_PRIVATE_URL/POSTGRES_URL)")
        # Webhook base URL: explicit, then platform-provided
        webhook_url = (
            os.getenv("WEBHOOK_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or (f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else None)
            or os.getenv("RAILWAY_STATIC_URL")
        )
        return cls(
            bot_token=_require("BOT_TOKEN"),
            database_url=database_url,
            openrouter_api_key=_require("OPENROUTER_API_KEY"),
            webhook_url=webhook_url,
            webhook_secret=os.getenv("WEBHOOK_SECRET"),
            webhook_path=os.getenv("WEBHOOK_PATH", "/webhook"),
            port=int(os.getenv("PORT", "8080")),
            bot_username=os.getenv("BOT_USERNAME"),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            model=os.getenv("MODEL", "gpt-4o-mini"),
            max_tokens=int(os.getenv("MAX_TOKENS", "1500")),
            temperature=float(os.getenv("TEMPERATURE", "0.7")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            daily_limit=int(os.getenv("DAILY_LIMIT", "10")),
            max_message_length=int(os.getenv("MAX_MESSAGE_LENGTH", "4000")),
            history_limit=int(os.getenv("HISTORY_LIMIT", "10")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config.from_env()