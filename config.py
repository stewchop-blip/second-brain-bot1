"""Configuration management for Second Brain Bot."""
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_path: str = "/webhook"
    port: int = 8080

    # Database
    database_url: str

    # LLM (OpenRouter)
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model: str = "openai/gpt-3.5-turbo"
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
        return cls(
            bot_token=_require("BOT_TOKEN"),
            webhook_url=os.getenv("WEBHOOK_URL"),
            webhook_secret=os.getenv("WEBHOOK_SECRET"),
            webhook_path=os.getenv("WEBHOOK_PATH", "/webhook"),
            port=int(os.getenv("PORT", "8080")),
            database_url=_require("DATABASE_URL"),
            openrouter_api_key=_require("OPENROUTER_API_KEY"),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            model=os.getenv("MODEL", "openai/gpt-3.5-turbo"),
            max_tokens=int(os.getenv("MAX_TOKENS", "1500")),
            temperature=float(os.getenv("TEMPERATURE", "0.7")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            daily_limit=int(os.getenv("DAILY_LIMIT", "20")),
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