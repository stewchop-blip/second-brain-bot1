"""
Main entry point for Second Brain Bot 1.5.
Web Service mode: aiohttp server with /webhook (Telegram) and /health (Render).
Single-chat core: no ConversationHandler, plain message dispatcher.
"""
import os
import logging
import asyncio

from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from config import get_config
from db import init_db, close_pool
from handlers import (
    start, menu_command, new_topic, help_command,
    what_callback, bonus_callback, legacy_callback,
    handle_message, handle_non_text, error_handler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=get_config().log_level,
)
logger = logging.getLogger(__name__)

config = get_config()


async def main() -> None:
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready")

    app = ApplicationBuilder().token(config.bot_token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("new", new_topic))
    app.add_handler(CommandHandler("help", help_command))

    # Callback queries (menu buttons) — only the new 1.5 callbacks
    app.add_handler(CallbackQueryHandler(new_topic, pattern=r"^new$"))
    app.add_handler(CallbackQueryHandler(what_callback, pattern=r"^what$"))
    app.add_handler(CallbackQueryHandler(bonus_callback, pattern=r"^bonus$"))
    app.add_handler(CallbackQueryHandler(help_command, pattern=r"^help$"))
    app.add_handler(CallbackQueryHandler(menu_command, pattern=r"^menu$"))
    # Legacy fallback for any old button callback
    app.add_handler(CallbackQueryHandler(legacy_callback))

    # Messages: text -> chat; anything else -> non-text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_non_text))

    app.add_error_handler(error_handler)

    await app.initialize()
    await app.start()

    webhook_url = config.webhook_url
    if not webhook_url:
        logger.error("No webhook URL available (set WEBHOOK_URL or use Render's RENDER_EXTERNAL_URL).")
        return
    full_url = f"{webhook_url.rstrip('/')}{config.webhook_path}"

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

    await app.bot.set_webhook(url=full_url, secret_token=config.webhook_secret)
    logger.info(f"Webhook registered at: {full_url}")

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logger.info(f"HTTP server listening on port {config.port}")

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
