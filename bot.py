"""
Main entry point for Second Brain Bot.
Web Service mode: aiohttp server with /webhook (Telegram) and /health (Render).
"""
import os
import logging
import asyncio

from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder

from config import get_config
from db import init_db, close_pool
from handlers import get_conversation_handler, error_handler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=get_config().log_level,
)
logger = logging.getLogger(__name__)

config = get_config()


async def main() -> None:
    # Initialize database
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready")

    # Build Telegram application
    app = ApplicationBuilder().token(config.bot_token).build()
    app.add_handler(get_conversation_handler())
    app.add_error_handler(error_handler)
    await app.initialize()
    await app.start()

    # Determine webhook URL (Render provides RENDER_EXTERNAL_URL automatically)
    webhook_url = config.webhook_url
    if not webhook_url:
        logger.error("No webhook URL available (set WEBHOOK_URL or use Render's RENDER_EXTERNAL_URL).")
        return
    full_url = f"{webhook_url.rstrip('/')}{config.webhook_path}"

    # --- HTTP handlers ---
    async def handle_webhook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400)
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response()

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    aio_app = web.Application()
    aio_app.router.add_post(config.webhook_path, handle_webhook)
    aio_app.router.add_get("/health", handle_health)

    # Register webhook with Telegram
    await app.bot.set_webhook(url=full_url, secret_token=config.webhook_secret)
    logger.info(f"Webhook registered at: {full_url}")

    # Start HTTP server
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logger.info(f"HTTP server listening on port {config.port}")

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        await app.bot.delete_webhook()
        await app.stop()
        await app.shutdown()
        await close_pool()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
