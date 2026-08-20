"""
Telegram handlers for Second Brain Bot 1.5 (simplified, robust).
Single-chat core: user writes -> one AI call returns JSON -> bot replies as plain text.
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
from prompts import humanize_prompt, HUMANIZE_LEVELS

logger = logging.getLogger(__name__)

config = get_config()
DAILY_LIMIT = config.daily_limit  # 10 per spec
BOT_USERNAME = getattr(config, "bot_username", None)
MAX_MESSAGE_LENGTH = getattr(config, "max_message_length", 4000)
# Hard cap for a single humanize request (keeps API cost bounded)
HUMANIZE_MAX_LENGTH = 8000


# ---------- Keyboards (minimal) ----------
def new_topic_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новая тема", callback_data="new")],
    ])


def humanize_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Очеловечить текст", callback_data="humanize")],
        [InlineKeyboardButton("🆕 Новая тема", callback_data="new")],
    ])


def humanize_again_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сделать ещё естественнее", callback_data="humanize_strong")],
        [InlineKeyboardButton("✨ Другой текст", callback_data="humanize")],
        [InlineKeyboardButton("🆕 Новая тема", callback_data="new")],
    ])


def bonus_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Получить бонусные вопросы", callback_data="bonus")],
    ])


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
    payload = None
    if context.args:
        payload = context.args[0]
    await get_or_create_user(user.id, user.username, user.first_name, source_param=payload)
    await clear_context(user.id)
    await set_awaiting_clarification(user.id, False)
    await analytics.track("user_started", user.id, {"source_param": payload, "is_new": True})

    text = (
        "🧠 Второй мозг\n\n"
        "Просто напиши мне задачу — и я помогу.\n\n"
        "Например:\n"
        "• «Стоит ли менять работу?»\n"
        "• «Как лучше ответить клиенту?»\n"
        "• «Помоги выбрать ноутбук»\n\n"
        "Что у тебя на уме?"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=humanize_keyboard())
    elif update.callback_query:
        await safe_edit(update.callback_query, text, humanize_keyboard())


# ---------- Menu ----------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "🧠 Второй мозг\n\nПросто напиши задачу — режим выбирать не нужно."
    if update.message:
        await update.message.reply_text(text, reply_markup=humanize_keyboard())
    elif update.callback_query:
        await safe_edit(update.callback_query, text, humanize_keyboard())


# ---------- New topic ----------
async def new_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await clear_context(user_id)
    await set_awaiting_clarification(user_id, False)
    context.user_data.pop("humanize_mode", None)
    text = "🆕 Начали новую тему.\n\nПредыдущий разговор больше не учитываю.\n\nО чём поговорим теперь?"
    if update.message:
        await update.message.reply_text(text, reply_markup=humanize_keyboard())
    elif update.callback_query:
        await safe_edit(update.callback_query, text, humanize_keyboard())


# ---------- Help ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "❓ Как пользоваться\n\n"
        "Просто напиши вопрос или опиши ситуацию обычными словами.\n\n"
        "Я сам пойму, нужно ли ответить на вопрос, проверить сообщение, разобрать идею или помочь выбрать.\n\n"
        "Хочешь сделать текст естественнее — нажми «✨ Очеловечить текст».\n\n"
        "Чтобы начать с чистого листа — нажми 🆕 Новая тема.\n\n"
        "Сейчас бот работает только с текстом."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=humanize_keyboard())
    elif update.callback_query:
        await safe_edit(update.callback_query, text, humanize_keyboard())


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
        "🎁 Бонусные вопросы\n\n"
        "Пригласи друга во «Второй мозг».\n\n"
        "Когда он отправит 3 запроса:\n"
        "• ты получишь +20 запросов\n"
        "• друг получит +10 запросов\n\n"
        f"Твоя ссылка: {ref_link}\n\n"
        f"Бонусных запросов: {user.get('bonus_balance', 0)}"
    )
    share_url = f"https://t.me/share/url?url={ref_link}&text=🧠 Попробуй «Второй мозг» — просто напиши ему вопрос обычными словами."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Поделиться с другом", url=share_url)]])
    await safe_edit(query, text, kb)


# ---------- Legacy fallback ----------
async def legacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await safe_edit(query,
        "Эта кнопка относится к старой версии бота 🙂\n\n"
        "Теперь просто напиши задачу обычным сообщением — ничего выбирать не нужно.")


# ---------- Humanizer ----------
async def humanize_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point: user chose «Очеловечить текст» (NORMAL level)."""
    query = update.callback_query
    await query.answer()
    context.user_data["humanize_mode"] = "NORMAL"
    text = "✨ Очеловечить текст\n\nПришли текст, который хочешь сделать естественнее 👇\n\n(Когда закончим — вернёмся к обычному общению.)"
    await safe_edit(query, text)


async def humanize_strong_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Сделать ещё естественнее» — deeper pass on the same text."""
    query = update.callback_query
    await query.answer()
    prev = context.user_data.get("humanize_last_text")
    if not prev:
        context.user_data["humanize_mode"] = "STRONG"
        await safe_edit(query, "✨ Пришли текст, который хочешь сделать естественнее 👇")
        return
    context.user_data["humanize_mode"] = "STRONG"
    # Re-process the last text at STRONG level directly
    await _process_humanize(update, context, prev, level="STRONG", edit=True)


async def _process_humanize(update, context, text: str, level: str = "NORMAL", edit: bool = False) -> None:
    user = update.effective_user
    user_id = user.id
    text = (text or "").strip()

    if not text:
        msg = update.callback_query.message if edit else update.message
        await msg.reply_text("Пустое сообщение. Пришли текст, который нужно переработать.")
        return

    if len(text) > HUMANIZE_MAX_LENGTH:
        msg = update.callback_query.message if edit else update.message
        await msg.reply_text(
            f"Текст слишком длинный (максимум {HUMANIZE_MAX_LENGTH} символов). "
            "Пришли его частями."
        )
        return

    # Rate-limit: same daily budget as normal chat
    if not await can_make_request(user_id, DAILY_LIMIT):
        msg = update.callback_query.message if edit else update.message
        await msg.reply_text(
            "🧠 На сегодня бесплатные запросы закончились.\n\n"
            "Завтра снова будет доступно 10.\n\n"
            "Продолжить раньше можно за бонусные запросы — пригласи друга.",
            reply_markup=bonus_button(),
        )
        return

    target_msg = update.callback_query.message if edit else update.message
    thinking = await target_msg.reply_text("✨ Делаю текст живее…")

    try:
        async with LLMClient() as client:
            result = await client.complete_plain(
                user_message=text,
                system_prompt=humanize_prompt(level),
                max_tokens=max(400, min(int(len(text) * 1.5) + 400, 2000)),
                temperature=0.7,
            )
    except (ModelError, RateLimitError) as e:
        logger.warning(f"Humanize LLM error for {user_id}: {e}")
        await thinking.delete()
        await target_msg.reply_text("⚠️ Не получилось переработать текст. Попробуй ещё раз.")
        return
    except Exception:
        logger.exception(f"Unexpected humanize error for {user_id}")
        await thinking.delete()
        await target_msg.reply_text("⚠️ Что-то пошло не так. Попробуй ещё раз.")
        return

    result = (result or "").strip()
    if not result:
        await thinking.delete()
        await target_msg.reply_text("⚠️ Не получилось переработать текст. Попробуй ещё раз.")
        return

    # Account usage (same budget as normal requests)
    used = await increment_daily(user_id)
    remaining = max(0, DAILY_LIMIT - used)
    await increment_total(user_id)
    await set_awaiting_clarification(user_id, False)

    context.user_data["humanize_last_text"] = text
    context.user_data["humanize_mode"] = None  # return to normal chat afterwards

    await thinking.delete()
    out = result
    if remaining <= 5 and remaining > 0:
        out += f"\n\n—\n🧠 Осталось запросов сегодня: {remaining}"
    await target_msg.reply_text(out, reply_markup=humanize_again_keyboard())
    await analytics.track("humanize", user_id, {"level": level, "success": True, "length": len(text)})


# ---------- Chat ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    text = (update.message.text or "").strip()
    if not text:
        return

    # Humanizer mode: route text to the humanizer pipeline
    if context.user_data.get("humanize_mode"):
        level = context.user_data.get("humanize_mode")
        if level not in HUMANIZE_LEVELS:
            level = "NORMAL"
        await _process_humanize(update, context, text, level=level)
        return

    # Length guard (defense against oversized/abusive requests)
    if len(text) > MAX_MESSAGE_LENGTH:
        await update.message.reply_text(
            f"Сообщение слишком длинное (максимум {MAX_MESSAGE_LENGTH} символов). "
            "Сократи или пришли по частям."
        )
        return

    lock = context.user_data.get("_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.user_data["_lock"] = lock
    if lock.locked():
        await update.message.reply_text("⏳ Подожди, предыдущий запрос ещё обрабатывается…")
        return

    async with lock:
        await _process_chat(update, user_id, text)


async def _process_chat(update: Update, user_id: int, text: str) -> None:
    if not await can_make_request(user_id, DAILY_LIMIT):
        await update.message.reply_text(
            "🧠 На сегодня бесплатные запросы закончились.\n\n"
            "Завтра снова будет доступно 10.\n\n"
            "Продолжить раньше можно за бонусные запросы — пригласи друга.",
            reply_markup=bonus_button(),
        )
        return

    state = await get_conversation_state(user_id)
    awaiting = state["awaiting_clarification"]
    history = await get_recent_conversations(user_id, limit=6)
    is_free = awaiting  # clarification answer costs nothing

    thinking = await update.message.reply_text("🧠 Думаю…")
    try:
        async with LLMClient() as client:
            reply = await client.complete(user_message=text, history=history)

        answer = (reply.answer or "").strip()
        if not answer:
            answer = "Не смог сформулировать ответ. Попробуй перефразировать вопрос."

        await save_conversation(user_id, reply.intent, text, answer, reply.total_tokens or 0)

        # Update context
        ctx = state["context"]
        ctx.append({"role": "user", "content": text})
        ctx.append({"role": "assistant", "content": answer})
        ctx = ctx[-12:]
        await save_context(user_id, ctx, awaiting_clarification=reply.needs_clarification)

        remaining = 0
        if not is_free:
            if await daily_remaining(user_id, DAILY_LIMIT) > 0:
                used = await increment_daily(user_id)
                remaining = max(0, DAILY_LIMIT - used)
            else:
                await consume_bonus(user_id)
                remaining = await bonus_balance(user_id)
            await increment_total(user_id)
            await set_awaiting_clarification(user_id, reply.needs_clarification)
            activated = await record_successful_request_for_referral(user_id)
            if activated:
                await update.message.reply_text("🎁 Тебе начислено +10 бонусных запросов за приглашённого друга!")
        else:
            await set_awaiting_clarification(user_id, reply.needs_clarification)

        # Compose answer as PLAIN TEXT (no Markdown to avoid parse errors)
        out = answer
        if reply.next_step:
            out += f"\n\n{reply.next_step}"

        if not is_free and remaining <= 5 and remaining > 0:
            out += f"\n\n—\n🧠 Осталось запросов сегодня: {remaining}"
        elif not is_free and remaining == 0:
            out += "\n\n🧠 Это был последний бесплатный запрос на сегодня. Завтра лимит обновится. Дополнительно — за приглашение друга."
            await thinking.delete()
            await update.message.reply_text(out, reply_markup=bonus_button())
            return

        await thinking.delete()
        await update.message.reply_text(out)

        await analytics.track("request", user_id, {
            "intent": reply.intent, "success": True, "model": config.model,
            "total_tokens": reply.total_tokens, "cost_usd": reply.cost_usd,
            "generation_id": reply.generation_id, "daily_remaining": remaining,
            "bonus_balance": await bonus_balance(user_id),
        })
        if not is_free:
            await analytics.track("feature_used", user_id, {"intent": reply.intent})

    except (ModelError, RateLimitError) as e:
        logger.warning(f"LLM error for {user_id}: {e}")
        await thinking.delete()
        await update.message.reply_text("⚠️ Не получилось получить ответ. Попробуй ещё раз.")
        await analytics.track("request", user_id, {"success": False, "error": str(e)[:100]})
    except Exception:
        logger.exception(f"Unexpected error for {user_id}")
        await thinking.delete()
        await update.message.reply_text("⚠️ Что-то пошло не так. Попробуй ещё раз или /start.")


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пока я умею работать только с текстом 🙂\n\n"
        "Напиши вопрос обычным сообщением — разберёмся."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.exception("Unhandled error", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так. Попробуй ещё раз или /start"
            )
        except Exception:
            pass
