"""
Configuration management for Second Brain Bot.
Loads everything from environment variables.

IMPORTANT: NEVER raise at import time. If a variable is missing, fall back to a
sensible default (or None) so that importing this module — and starting the
HTTP server for platform healthchecks — never crashes.
"""
import os


def _env(key: str, default=None):
    val = os.getenv(key)
    if val is None or val == "":
        return default
    return val


# --- Required secrets (no hard crash if absent; checked where used) ---
BOT_TOKEN = _env("BOT_TOKEN")
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")

# --- Database (Railway provides DATABASE_URL; fall back to common names) ---
DATABASE_URL = (
    _env("DATABASE_URL")
    or _env("DATABASE_PRIVATE_URL")
    or _env("POSTGRES_URL")
    or _env("POSTGRES_PRIVATE_URL")
)

# --- Webhook base URL ---
WEBHOOK_HOST = (
    _env("WEBHOOK_URL")
    or _env("RENDER_EXTERNAL_URL")
    or (f"https://{_env('RAILWAY_PUBLIC_DOMAIN')}" if _env("RAILWAY_PUBLIC_DOMAIN") else None)
    or _env("RAILWAY_STATIC_URL")
)
WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
WEBHOOK_PATH = _env("WEBHOOK_PATH", "/webhook")
PORT = int(_env("PORT", "8080"))
BOT_USERNAME = _env("BOT_USERNAME")

# --- LLM (OpenRouter) ---
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = _env("MODEL", "gpt-4o-mini")
MAX_TOKENS = int(_env("MAX_TOKENS", "1500"))
TEMPERATURE = float(_env("TEMPERATURE", "0.7"))
REQUEST_TIMEOUT = int(_env("REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(_env("MAX_RETRIES", "3"))

# --- Limits ---
DAILY_LIMIT = int(_env("DAILY_LIMIT", "10"))
MAX_MESSAGE_LENGTH = int(_env("MAX_MESSAGE_LENGTH", "4000"))
HISTORY_LIMIT = int(_env("HISTORY_LIMIT", "10"))

LOG_LEVEL = _env("LOG_LEVEL", "INFO")


# Backwards-compatible accessor: returns an object with attribute access.
# Used by bot.py / handlers.py via `config = get_config()`.
from types import SimpleNamespace

def get_config():
    return SimpleNamespace(
        bot_token=BOT_TOKEN,
        openrouter_api_key=OPENROUTER_API_KEY,
        database_url=DATABASE_URL,
        webhook_url=WEBHOOK_HOST,
        webhook_secret=WEBHOOK_SECRET,
        webhook_path=WEBHOOK_PATH,
        port=PORT,
        bot_username=BOT_USERNAME,
        openrouter_base_url=OPENROUTER_BASE_URL,
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        request_timeout=REQUEST_TIMEOUT,
        max_retries=MAX_RETRIES,
        daily_limit=DAILY_LIMIT,
        max_message_length=MAX_MESSAGE_LENGTH,
        history_limit=HISTORY_LIMIT,
        log_level=LOG_LEVEL,
    )
