"""
Telegram handlers for Second Brain Bot 1.5.
Single-chat core: user writes -> one AI call returns JSON -> bot replies.
No mode buttons in the user path. Only 🆕 Новая тема.
"""
import logging
import asyncio
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

from config import get_config
from db import (
    get_or_create_user, daily_remaining, bonus_balance, can_make_request,
    increment_daily, increment_total, consume_bonus, get_conversation_state,
    save_context, clear_context, set_awaiting_clarification, record_successful_request_for_referral,
    save_conversation, get_recent_conversations, get_referral_token, get_user,
)
from llm_client import LLMClient, ModelError, RateLimitError
from analytics import analytics

logger = logging.getLogger(__name__)

config = get_config()
DAILY_LIMIT = config.daily_limit  # 10 per spec
BOT_USERNAME = config.bot_username if hasattr(config, "bot_username") else None


# ---------- Keyboards ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Что можно спросить?", callback_data="what")],
        [InlineKeyboardButton("🎁 Бонусные вопросы", callback_data="bonus")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ])


def new_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Что можно спросить?", callback_data="what")],
        [InlineKeyboardButton("🎁 Бонусные вопросы", callback_data="bonus")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ])


def no_keyboard() -> None:
    return None


# ---------- Helpers ----------
async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.debug(f"edit failed: {e}")
        try:
            await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass


# ---------- Start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    # Deep link /start=<payload>
    payload = None
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            payload = payload  # store as source_param
    await get_or_create_user(user.id, user.username, user.first_name, source_param=payload)

    # Reset context on /start
    await clear_context(user.id)
    await set_awaiting_clarification(user.id, False)

    await analytics.track("user_started", user.id, {"source_param": payload, "is_new": True})

    text = (
        "🧠 **Второй мозг**\n\n"
        "Помогу разобраться, придумать, написать или принять решение.\n\n"
        "Никаких сложных команд и выбора нейросетей — просто напиши, что тебе нужно.\n\n"
        "Например:\n"
        "• «Стоит ли менять работу?»\n"
        "• «Как лучше ответить клиенту?»\n"
        "• «Помоги выбрать между двумя ноутбуками»\n"
        "• «Хочу открыть кофейню — разнеси идею по фактам»\n\n"
        f"🎁 У тебя {DAILY_LIMIT} бесплатных запросов в день.\n\n"
        "Что у тебя на уме?"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await safe_edit(update.callback_query, text, main_menu_keyboard(), "Markdown")


# ---------- Menu ----------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🧠 **Второй мозг**\n\n"
        "Просто напиши мне задачу — режим выбирать не нужно."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новая тема", callback_data="new")],
        [InlineKeyboardButton("✨ Что можно спросить?", callback_data="what")],
        [InlineKeyboardButton("🎁 Бонусные вопросы", callback_data="bonus")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ])
    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    elif update.callback_query:
        await safe_edit(update.callback_query, text, kb, "Markdown")


# ---------- New topic ----------
async def new_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await clear_context(user_id)
    await set_awaiting_clarification(user_id, False)
    text = (
        "🆕 Начали новую тему.\n\n"
        "Предыдущий разговор больше не учитываю.\n\n"
        "О чём поговорим теперь?"
    )
    if update.message:
        await update.message.reply_text(text)
    elif update.callback_query:
        await safe_edit(update.callback_query, text)


# ---------- Help ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "❓ **Как пользоваться Вторым мозгом**\n\n"
        "Просто напиши вопрос или опиши ситуацию обычными словами.\n\n"
        "Я сам пойму, нужно ли:\n"
        "• ответить на вопрос;\n"
        "• проверить сообщение;\n"
        "• разобрать идею;\n"
        "• помочь выбрать.\n\n"
        "Чтобы начать разговор с чистого листа — нажми 🆕 Новая тема.\n\n"
        "Сейчас бот работает только с текстом. Фото, файлы и голосовые пока не обрабатываются."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆕 Новая тема", callback_data="new")]])
    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    elif update.callback_query:
        await safe_edit(update.callback_query, text, kb, "Markdown")


# ---------- What can I ask ----------
async def what_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        "✨ **Что можно спросить?**\n\n"
        "Практически любую текстовую задачу.\n\n"
        "💬 Разобраться: «Почему я постоянно откладываю это дело?»\n"
        "✍️ Проверить сообщение: «Вот что хочу написать начальнику. Нормально звучит?»\n"
        "💡 Проверить идею: «Думаю открыть маленькую кофейню. Где слабые места?»\n"
        "⚖️ Помочь выбрать: «Что лучше для меня: iPhone или Samsung?»\n"
        "🧠 Обычный вопрос: «Объясни ипотеку простыми словами».\n\n"
        "Просто напиши задачу своими словами. Режим выбирать не нужно."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆕 Новая тема", callback_data="new")]])
    await safe_edit(query, text, kb, "Markdown")


# ---------- Bonus ----------
async def bonus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    token = await get_referral_token(user_id)
    bot_uname = BOT_USERNAME or "secondbrainbot"
    ref_link = f"https://t.me/{bot_uname}?start=ref_{token}"
    user = await get_user(user_id)
    text = (
        "🎁 **Бонусные вопросы**\n\n"
        "Пригласи друга во «Второй мозг».\n\n"
        "Когда он начнёт реально пользоваться ботом и отправит 3 запроса:\n"
        "• ты получишь +20 запросов\n"
        "• друг получит +10 запросов\n\n"
        f"Твоя ссылка: {ref_link}\n\n"
        f"Приглашено: {user.get('referrer_id', '—')}\n"
        f"Бонусных запросов: {user.get('bonus_balance', 0)}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться с другом", url=f"https://t.me/share/url?url={ref_link}&text=🧠 Я пользуюсь «Вторым мозгом» — можно просто написать ему вопрос или ситуацию обычными словами. Попробуй:")],
    ])
    await safe_edit(query, text, kb, "Markdown")


# ---------- Legacy fallback ----------
async def legacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await safe_edit(query,
        "Эта кнопка относится к старой версии бота 🙂\n\n"
        "Теперь ничего выбирать не нужно — просто напиши задачу обычным сообщением.")


# ---------- Chat message handler ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if not text:
        return

    # Per-user lock to avoid duplicate concurrent requests
    lock = context.user_data.get("_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.user_data["_lock"] = lock
    if lock.locked():
        await update.message.reply_text("⏳ Подожди, предыдущий запрос ещё обрабатывается…")
        return

    async with lock:
        await _process_chat(update, context, user_id, text)


async def _process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    # Limits
    if not await can_make_request(user_id, DAILY_LIMIT):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎁 Получить бонусные вопросы", callback_data="bonus")]])
        await update.message.reply_text(
            "🧠 На сегодня бесплатные запросы закончились.\n\n"
            "Завтра снова будет доступно 10.\n\n"
            "Продолжить раньше можно за бонусные запросы — пригласи друга во «Второй мозг».",
            reply_markup=kb,
        )
        return

    # Load conversation state
    state = await get_conversation_state(user_id)
    awaiting = state["awaiting_clarification"]

    # Build context (last ~6 pairs)
    history = await get_recent_conversations(user_id, limit=6)
    # If awaiting clarification, this message is a free follow-up (no charge)
    is_free = awaiting

    thinking = await update.message.reply_text("🧠 Думаю…")
    try:
        async with LLMClient() as client:
            reply = await client.complete(user_message=text, history=history)

        # Persist
        mode = reply.intent
        await save_conversation(user_id, mode, text, reply.answer, reply.total_tokens or 0)

        # Update context (in-memory + DB)
        ctx = state["context"]
        ctx.append({"role": "user", "content": text})
        ctx.append({"role": "assistant", "content": reply.answer})
        ctx = ctx[-12:]  # keep last 6 pairs
        await save_context(user_id, ctx, awaiting_clarification=reply.needs_clarification)

        # Charge limit only if not a free clarification answer
        remaining = 0
        if not is_free:
            # consume daily first, else bonus
            if await daily_remaining(user_id, DAILY_LIMIT) > 0:
                used = await increment_daily(user_id)
                remaining = max(0, DAILY_LIMIT - used)
            else:
                await consume_bonus(user_id)
                remaining = await bonus_balance(user_id)
            await increment_total(user_id)
            await set_awaiting_clarification(user_id, reply.needs_clarification)
            # referral bonus check
            activated = await record_successful_request_for_referral(user_id)
            if activated:
                await update.message.reply_text("🎁 Готово! За приглашение тебе начислено +10 дополнительных запросов.")
        else:
            # this was a clarification answer, no charge; clear flag
            await set_awaiting_clarification(user_id, reply.needs_clarification)

        await analytics.track("request", user_id, {
            "intent": reply.intent,
            "success": True,
            "model": config.model,
            "total_tokens": reply.total_tokens,
            "cost_usd": reply.cost_usd,
            "generation_id": reply.generation_id,
            "daily_remaining": remaining,
            "bonus_balance": await bonus_balance(user_id),
        })
        if not is_free:
            await analytics.track("feature_used", user_id, {"intent": reply.intent})

        # Build answer text
        answer_text = reply.answer
        if reply.next_step:
            answer_text += f"\n\n{reply.next_step}"

        # Limit counter display only when remaining <= 5
        if not is_free and remaining <= 5 and remaining > 0:
            answer_text += f"\n\n—\n🧠 Осталось запросов сегодня: {remaining}"
        elif not is_free and remaining == 0:
            answer_text += "\n\n🧠 Это был последний бесплатный запрос на сегодня.\nНовый лимит появится завтра. Дополнительные запросы можно получить за приглашение друга."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎁 Получить бонусные вопросы", callback_data="bonus")]])
            await thinking.delete()
            await update.message.reply_text(answer_text, reply_markup=kb, parse_mode="Markdown")
            return

        await thinking.delete()
        await update.message.reply_text(answer_text, parse_mode="Markdown")

    except (ModelError, RateLimitError) as e:
        logger.warning(f"LLM error for {user_id}: {e}")
        await thinking.delete()
        await update.message.reply_text("⚠️ Не получилось получить ответ. Попробуйте ещё раз.")
        await analytics.track("request", user_id, {"success": False, "error": str(e)[:100]})
    except Exception as e:
        logger.exception(f"Unexpected error for {user_id}")
        await thinking.delete()
        await update.message.reply_text("⚠️ Что-то пошло не так. Попробуйте ещё раз или /start")


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos, voice, docs, stickers — text only."""
    await update.message.reply_text(
        "Пока я умею работать только с текстом 🙂\n\n"
        "Напиши вопрос обычным сообщением — разберёмся."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Что-то пошло не так. Попробуй ещё раз или /start")
        except Exception:
            pass
