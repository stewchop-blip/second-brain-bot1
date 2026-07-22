"""
Main entry point for Second Brain Bot.
Supports both webhook (production) and polling (local dev).
"""
import os
import logging
import asyncio
from contextlib import asynccontextmanager

from telegram.ext import Application, ApplicationBuilder

from config import get_config
from db import init_db, close_pool
from handlers import get_conversation_handler, error_handler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=get_config().log_level,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: Application):
    """Manage application lifecycle."""
    # Initialize database
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready")
    
    yield
    
    # Cleanup
    logger.info("Closing database pool...")
    await close_pool()
    logger.info("Shutdown complete")


async def run_polling(app: Application) -> None:
    """Run bot in polling mode (local dev)."""
    logger.info("Starting bot in polling mode...")
    await app.run_polling()


async def run_webhook(app: Application, config) -> None:
    """Run bot in webhook mode (production)."""
    webhook_url = config.webhook_url
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL must be set for webhook mode")
    
    webhook_secret = config.webhook_secret or "default-secret"
    
    logger.info(f"Starting bot in webhook mode: {webhook_url}{config.webhook_path}")
    
    await app.run_webhook(
        listen="0.0.0.0",
        port=config.port,
        url_path=config.webhook_path,
        secret_token=webhook_secret,
        webhook_url=f"{webhook_url}{config.webhook_path}",
        drop_pending_updates=True,
    )


async def main() -> None:
    """Main entry point."""
    config = get_config()
    
    # Build application
    app = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init([])  # We use lifespan instead
        .build()
    )
    
    # Add lifespan
    app.post_init = lifespan
    
    # Add handlers
    app.add_handler(get_conversation_handler())
    
    # Add error handler
    app.add_error_handler(error_handler)
    
    # Run in appropriate mode
    if config.webhook_url:
        await run_webhook(app, config)
    else:
        await run_polling(app)


if __name__ == "__main__":
    asyncio.run(main())