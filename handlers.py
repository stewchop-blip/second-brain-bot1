"""
Telegram bot handlers.
"Спросить" is the main universal chat. Specialized modes are optional add-ons.
Context is kept per-conversation in user_data; history is listed from DB.
"""
import logging
import asyncio
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from config import get_config
from db import (
    get_or_create_user,
    check_limit,
    increment_usage,
    get_remaining,
    save_conversation,
    get_recent_conversations,
    get_user_topics,
)
from llm_client import LLMClient, ModelError, RateLimitError
from prompts import get_mode

logger = logging.getLogger(__name__)

# Conversation states
SELECTING, ASK_INPUT, IDEA_INPUT, MESSAGE_INPUT, CHOOSE_INPUT = range(5)

config = get_config()

MODE_LABELS = {
    "check_idea": "💡 Проверить идею",
    "check_message": "✉️ Проверить сообщение",
    "help_choose": "⚖️ Помочь выбрать",
}


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu — 'Спросить' is the primary action, others secondary."""
    keyboard = [
        [
            InlineKeyboardButton(
                "❓ Спросить\nЗадайте любой вопрос или опишите ситуацию.",
                callback_data="m:ask",
            )
        ],
        [
            InlineKeyboardButton(
                "✉️ Проверить сообщение\nПроверьте текст перед отправкой.",
                callback_data="m:check_message",
            )
        ],
        [
            InlineKeyboardButton("📂 Ещё возможности", callback_data="more"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def more_keyboard() -> InlineKeyboardMarkup:
    """Secondary menu — specialized modes + history."""
    keyboard = [
        [
            InlineKeyboardButton("💡 Проверить идею\nНайдём слабые места и риски.", callback_data="m:check_idea"),
        ],
        [
            InlineKeyboardButton("⚖️ Помочь выбрать\nСравним варианты и разберёмся, что выбрать.", callback_data="m:help_choose"),
        ],
        [
            InlineKeyboardButton("🕘 История", callback_data="history"),
            InlineKeyboardButton("⬅️ Назад", callback_data="menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")],
    ])


def ask_followup_keyboard() -> InlineKeyboardMarkup:
    """After an 'ask' answer — continue, deeper, new, menu."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Продолжить", callback_data="continue"),
            InlineKeyboardButton("🔎 Подробнее", callback_data="deeper"),
        ],
        [
            InlineKeyboardButton("🆕 Новый вопрос", callback_data="new"),
            InlineKeyboardButton("⬅️ Главное меню", callback_data="menu"),
        ],
    ])


def ask_route_keyboard() -> InlineKeyboardMarkup:
    """Optional specialized check on the same text (not forced)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💡 Проверить идею", callback_data="route:check_idea"),
        ],
        [
            InlineKeyboardButton("✉️ Проверить сообщение", callback_data="route:check_message"),
        ],
        [
            InlineKeyboardButton("⚖️ Помочь выбрать", callback_data="route:help_choose"),
        ],
        [
            InlineKeyboardButton("✅ Просто ответить", callback_data="continue"),
        ],
    ])


def specialized_followup_keyboard() -> InlineKeyboardMarkup:
    """After a specialized answer — continue in same mode, new, menu."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Продолжить", callback_data="continue"),
            InlineKeyboardButton("🔎 Подробнее", callback_data="deeper"),
        ],
        [
            InlineKeyboardButton("🆕 Новый вопрос", callback_data="new"),
            InlineKeyboardButton("⬅️ Главное меню", callback_data="menu"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — heading + primary question."""
    user = update.effective_user
    await get_or_create_user(user.id, user.username, user.first_name)

    # Reset conversation context on fresh start
    context.user_data["conv"] = []
    context.user_data["pending_text"] = None
    context.user_data["active_mode"] = None

    text = (
        "🧠 **Второй мозг**\n\n"
        "Просто напишите вопрос или расскажите, с чем нужно разобраться.\n\n"
        "**Что нужно сделать?**"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await safe_edit(update.callback_query, text, main_menu_keyboard())

    return SELECTING


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """How to use — simple."""
    text = (
        "📖 **Как пользоваться**\n\n"
        "**❓ Спросить** — напиши любой вопрос обычными словами. Я отвечу и запомню контекст, "
        "чтобы можно было уточнять и продолжать тему.\n\n"
        "**✉️ Проверить сообщение** — вставь текст для отправки, я покажу, как его воспримут, и дам улучшенную версию.\n\n"
        "**💡 Проверить идею** — расскажи идею, найду риски и слабые места.\n\n"
        "**⚖️ Помочь выбрать** — напиши варианты через «или».\n\n"
        "После ответа нажми **💬 Продолжить**, **🔎 Подробнее** или **🆕 Новый вопрос**.\n\n"
        f"📝 Макс. длина: {config.max_message_length} символов"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await safe_edit(update.callback_query, text, back_keyboard())
    return SELECTING


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back to menu from any state — stable."""
    query = update.callback_query
    await query.answer()
    # Reset context when returning to menu
    context.user_data["conv"] = []
    context.user_data["pending_text"] = None
    context.user_data["active_mode"] = None
    await safe_edit(query, None, main_menu_keyboard(), menu_text=True)
    return SELECTING


async def more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "**Ещё возможности:**", more_keyboard())
    return SELECTING


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        topics = await get_user_topics(user_id, limit=10)
    except Exception as e:
        logger.exception("history error")
        topics = []
    if not topics:
        text = "🕘 **История**\n\nПока пусто. Задай вопрос — он появится здесь."
    else:
        lines = []
        for t in topics:
            label = t["first_message"][:40] + ("…" if len(t["first_message"]) > 40 else "")
            lines.append(f"• {label}")
        text = "🕘 **История**\n\n" + "\n".join(lines)
    await safe_edit(query, text, back_keyboard())
    return SELECTING


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mode selection — friendly prompt with examples."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    mode_key = query.data.split(":")[1]

    if not await check_limit(user_id, config.daily_limit):
        remaining = await get_remaining(user_id, config.daily_limit)
        await safe_edit(query,
            f"⛔ **Лимит на сегодня исчерпан**\n\n"
            f"Ты использовал все {config.daily_limit} запросов.\n"
            f"Возвращайся завтра! 🌅\n\n"
            f"Осталось: {remaining}",
            back_keyboard())
        return SELECTING

    context.user_data["active_mode"] = mode_key
    context.user_data["conv"] = []  # fresh context for this mode

    prompts = {
        "ask": (
            "❓ **Спросить**\n\n"
            "Напиши вопрос или расскажи ситуацию обычными словами.\n\n"
            "*Например:*\n"
            "• «Стоит ли менять работу?»\n"
            "• «Как лучше ответить клиенту?»\n"
            "• «Хочу открыть кофейню»"
        ),
        "check_idea": (
            "💡 **Проверить идею**\n\n"
            "Расскажи свою идею одним сообщением — я найду риски и слабые места.\n\n"
            "*Например:*\n"
            "• «Хочу открыть кофейню»\n"
            "• «Стоит ли менять работу?»"
        ),
        "check_message": (
            "✉️ **Проверить сообщение**\n\n"
            "Вставь сообщение, которое хочешь отправить — покажу, как его воспримут.\n\n"
            "*Например:*\n"
            "• «Привет. Хотел уточнить про встречу»\n"
            "• «Спасибо за предложение, но я подумаю»"
        ),
        "help_choose": (
            "⚖️ **Помочь выбрать**\n\n"
            "Напиши варианты, между которыми выбираешь.\n\n"
            "*Например:*\n"
            "• «Покупать эту машину или брать в лизинг?»\n"
            "• «Остаться в офисе или на удалёнке?»"
        ),
    }

    state_map = {
        "ask": ASK_INPUT,
        "check_idea": IDEA_INPUT,
        "check_message": MESSAGE_INPUT,
        "help_choose": CHOOSE_INPUT,
    }

    await safe_edit(query, prompts[mode_key], back_keyboard())
    return state_map[mode_key]


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose to run specialized mode on the text from 'Спросить' (optional, not forced)."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    target = query.data.split(":")[1]
    saved_text = context.user_data.get("pending_text", "")
    if not saved_text:
        await safe_edit(query, "Текст не сохранился. Выбери действие и напиши заново.", back_keyboard())
        return SELECTING
    intro = {
        "check_idea": "💡 Проверяю идею…",
        "check_message": "✉️ Проверяю сообщение…",
        "help_choose": "⚖️ Сравниваю варианты…",
    }[target]
    context.user_data["active_mode"] = target
    await _process_input_text(user_id, saved_text, target, intro, context, query=query)
    return SELECTING


async def continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Resume conversation — just prompt for next message in current mode."""
    query = update.callback_query
    await query.answer()
    mode = context.user_data.get("active_mode", "ask")
    hints = {
        "ask": "Напиши следующий вопрос или уточнение — я помню контекст.",
        "check_idea": "Расскажи ещё об идее или уточни детали.",
        "check_message": "Вставь следующий текст для проверки.",
        "help_choose": "Опиши варианты или добавь детали для сравнения.",
    }
    await safe_edit(query, hints.get(mode, "Напиши следующий вопрос."), back_keyboard())
    return _state_for_mode(mode)


async def deeper_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask AI to go deeper on the last answer."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    conv = context.user_data.get("conv", [])
    if not conv:
        await safe_edit(query, "Нет предыдущего ответа, чтобы углубиться. Задай вопрос.", back_keyboard())
        return SELECTING
    mode = context.user_data.get("active_mode", "ask")
    await _process_input_text(
        user_id,
        "Пожалуйста, расскажи подробнее и глубже про то, что ты написал выше. Раскрой детали.",
        mode,
        "🔎 Углубляюсь…",
        context,
        query=query,
    )
    return SELECTING


async def new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a new topic — clear context."""
    query = update.callback_query
    await query.answer()
    context.user_data["conv"] = []
    context.user_data["pending_text"] = None
    context.user_data["active_mode"] = None
    await safe_edit(query, "🆕 Новый вопрос. Контекст очищен — пиши с чистого листа.", main_menu_keyboard())
    return SELECTING


async def handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_text(update, context, "ask")


async def handle_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_text(update, context, "check_idea")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_text(update, context, "check_message")


async def handle_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_text(update, context, "help_choose")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, mode_key: str) -> int:
    """Shared text handler — keeps context, calls AI, shows optional routing."""
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    # Per-user lock: prevent duplicate concurrent requests
    lock = context.user_data.get("_lock")
    if lock and lock.locked():
        await update.message.reply_text("⏳ Подожди, предыдущий запрос ещё обрабатывается…", reply_markup=back_keyboard())
        return _state_for_mode(mode_key)
    if lock is None:
        lock = asyncio.Lock()
        context.user_data["_lock"] = lock

    async with lock:
        if len(text) > config.max_message_length:
            await update.message.reply_text(
                f"⚠️ Слишком длинно. Максимум {config.max_message_length} символов.\n"
                f"У тебя: {len(text)}. Сократи и попробуй снова.",
                reply_markup=back_keyboard(),
            )
            return _state_for_mode(mode_key)

        context.user_data["active_mode"] = mode_key
        context.user_data["pending_text"] = text

        intro = {
            "ask": "🧠 Думаю…",
            "check_idea": "💡 Проверяю идею…",
            "check_message": "✉️ Проверяю сообщение…",
            "help_choose": "⚖️ Сравниваю варианты…",
        }[mode_key]

        thinking = await update.message.reply_text(intro)
        await _process_input_text(user_id, text, mode_key, None, context, thinking=thinking, update=update)
    return SELECTING


async def _process_input_text(
    user_id: int,
    text: str,
    mode_key: str,
    intro: Optional[str],
    context: ContextTypes.DEFAULT_TYPE,
    thinking=None,
    update: Optional[Update] = None,
    query=None,
) -> None:
    """Core AI call. Used by both text messages and button-driven routes."""
    try:
        mode = get_mode(mode_key)
        # Build context from in-memory conversation (current topic)
        conv = context.user_data.get("conv", [])
        history = [{"user_message": m["u"], "bot_response": m["b"]} for m in conv[-config.history_limit:]]

        async with LLMClient() as client:
            response = await client.complete(
                system_prompt=mode.prompt,
                user_message=text,
                history=history,
            )

        await increment_usage(user_id)
        remaining = await get_remaining(user_id, config.daily_limit)

        # Save to DB and to in-memory context
        await save_conversation(
            user_id=user_id,
            mode=mode_key,
            user_message=text,
            bot_response=response.content,
            tokens_used=response.tokens_used,
        )
        conv.append({"u": text, "b": response.content})
        context.user_data["conv"] = conv[-config.history_limit:]

        footer = f"\n\n—\n📊 Осталось сегодня: **{remaining}** запросов"

        if mode_key == "ask":
            content = response.content + footer
            # Optional (not forced) specialized check suggestion
            suggestion = _suggest_route(text)
            if suggestion:
                content += (
                    f"\n\nЕсли хочешь, могу отдельно проверить это как «{MODE_LABELS[suggestion].split()[1]}» "
                    f"— нажми кнопку ниже."
                )
                kb = ask_route_keyboard()
            else:
                kb = ask_followup_keyboard()
        else:
            content = response.content + footer
            kb = specialized_followup_keyboard()

        if thinking:
            await thinking.delete()
        if update:
            await update.message.reply_text(content, reply_markup=kb, parse_mode="Markdown")
        elif query:
            await safe_edit(query, content, kb)

    except (ModelError, RateLimitError) as e:
        logger.warning(f"LLM error for user {user_id}: {e}")
        msg = "⚠️ Не получилось получить ответ. Попробуйте ещё раз."
        if thinking:
            await thinking.delete()
            await update.message.reply_text(msg, reply_markup=ask_followup_keyboard(), parse_mode="Markdown")
        elif query:
            await safe_edit(query, msg, back_keyboard())
    except Exception as e:
        logger.exception(f"Unexpected error for user {user_id}")
        msg = "⚠️ Что-то пошло не так. Попробуйте ещё раз или вернитесь в меню."
        if thinking:
            await thinking.delete()
            await update.message.reply_text(msg, reply_markup=back_keyboard(), parse_mode="Markdown")
        elif query:
            await safe_edit(query, msg, back_keyboard())


def _suggest_route(text: str) -> Optional[str]:
    """Lightweight heuristic suggestion (not forced routing)."""
    t = text.lower()
    if any(w in t for w in ["выбрать", "или ", "между", "сравн", "какой лучше", "брать или"]):
        return "help_choose"
    if any(w in t for w in ["открыть", "запустить", "идея", "бизнес", "план", "проект", "хочу "]):
        return "check_idea"
    # check_message suggestion only when it looks like a draft to send
    if len(text) > 20 and any(w in t for w in ["привет", "здравств", "напиши", "отправ", "скажи", "ответ"]):
        return "check_message"
    return None


def _state_for_mode(mode_key: str) -> int:
    return {
        "ask": ASK_INPUT,
        "check_idea": IDEA_INPUT,
        "check_message": MESSAGE_INPUT,
        "help_choose": CHOOSE_INPUT,
    }.get(mode_key, ASK_INPUT)


async def safe_edit(query, text, reply_markup, menu_text: bool = False):
    """Edit message text safely; fallback to new message if edit fails."""
    if menu_text and text is None:
        text = (
            "🧠 **Второй мозг**\n\n"
            "Просто напишите вопрос или расскажите, с чем нужно разобраться.\n\n"
            "**Что нужно сделать?**"
        )
    try:
        if isinstance(reply_markup, InlineKeyboardMarkup):
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await query.edit_message_text(text=text, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"edit_message_text failed: {e}")
        try:
            await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel."""
    await update.message.reply_text("Ок, вернулись в меню.", reply_markup=main_menu_keyboard())
    return SELECTING


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler."""
    logger.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так. Попробуй ещё раз или /start",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            pass


def get_conversation_handler() -> ConversationHandler:
    """Build main ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("menu", menu_callback),
            CallbackQueryHandler(mode_callback, pattern=r"^m:"),
            CallbackQueryHandler(route_callback, pattern=r"^route:"),
            CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
            CallbackQueryHandler(more_callback, pattern=r"^more$"),
            CallbackQueryHandler(history_callback, pattern=r"^history$"),
            CallbackQueryHandler(continue_callback, pattern=r"^continue$"),
            CallbackQueryHandler(deeper_callback, pattern=r"^deeper$"),
            CallbackQueryHandler(new_callback, pattern=r"^new$"),
            CallbackQueryHandler(help_command, pattern=r"^help$"),
        ],
        states={
            SELECTING: [
                CallbackQueryHandler(mode_callback, pattern=r"^m:"),
                CallbackQueryHandler(route_callback, pattern=r"^route:"),
                CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
                CallbackQueryHandler(more_callback, pattern=r"^more$"),
                CallbackQueryHandler(history_callback, pattern=r"^history$"),
                CallbackQueryHandler(continue_callback, pattern=r"^continue$"),
                CallbackQueryHandler(deeper_callback, pattern=r"^deeper$"),
                CallbackQueryHandler(new_callback, pattern=r"^new$"),
                CallbackQueryHandler(help_command, pattern=r"^help$"),
            ],
            ASK_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ask),
                CommandHandler("cancel", cancel),
            ],
            IDEA_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_idea),
                CommandHandler("cancel", cancel),
            ],
            MESSAGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
                CommandHandler("cancel", cancel),
            ],
            CHOOSE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choose),
                CommandHandler("cancel", cancel),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
        ],
        name="second_brain_conversation",
        persistent=False,
    )
