"""
Main entry point for Second Brain Bot 1.5.
Web Service mode: aiohttp server with /webhook (Telegram) and /health + / (Railway/Render).
Single-chat core: no ConversationHandler, plain message dispatcher.

CRITICAL for platform healthchecks (Railway/Render):
  The HTTP server MUST be started FIRST and kept alive even if the Telegram
  app init or webhook registration fails. Otherwise the platform healthcheck
  sees "service unavailable" and kills the container.
"""
import os
import logging
import asyncio
import traceback

from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from config import get_config
from db import init_db, close_pool
from handlers import (
    start, menu_command, new_topic, help_command,
    bonus_callback, legacy_callback,
    handle_message, handle_non_text, error_handler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

config = get_config()


async def main() -> None:
    # ---- Build the Telegram application (no start yet) ----
    app = ApplicationBuilder().token(config.bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("new", new_topic))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(new_topic, pattern=r"^new$"))
    app.add_handler(CallbackQueryHandler(bonus_callback, pattern=r"^bonus$"))
    app.add_handler(CallbackQueryHandler(help_command, pattern=r"^help$"))
    app.add_handler(CallbackQueryHandler(menu_command, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(legacy_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_non_text))
    app.add_error_handler(error_handler)

    # ---- HTTP server (start FIRST so healthcheck always passes) ----
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

    async def handle_root(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    aio_app = web.Application()
    aio_app.router.add_post(config.webhook_path, handle_webhook)
    aio_app.router.add_get("/health", handle_health)
    aio_app.router.add_get("/", handle_root)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logger.info(f"HTTP server listening on port {config.port} (healthcheck should pass now)")

    # ---- Now initialize Telegram app + DB + webhook (best-effort) ----
    try:
        logger.info("Initializing database...")
        await init_db()
        logger.info("Database ready")

        await app.initialize()
        await app.start()

        webhook_url = config.webhook_url
        if webhook_url:
            full_url = f"{webhook_url.rstrip('/')}{config.webhook_path}"
            await app.bot.set_webhook(url=full_url, secret_token=config.webhook_secret)
            logger.info(f"Webhook registered at: {full_url}")
        else:
            logger.warning("WEBHOOK_URL not set — webhook NOT registered.")
    except Exception:
        logger.error("Startup of Telegram/DB/webhook failed (server still running for healthcheck):\n"
                     + traceback.format_exc())

    # ---- Keep the process alive ----
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        try:
            await app.bot.delete_webhook()
        except Exception:
            pass
        try:
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        await close_pool()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
