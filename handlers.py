"""
Telegram bot handlers.
Problem-first UX, no "режим"/"AI" words, "Спросить" universal entry point
with smart routing into the three specialized modes.
"""
import logging
import re

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
    get_user_stats,
)
from llm_client import LLMClient
from prompts import get_mode

logger = logging.getLogger(__name__)

# Conversation states
SELECTING, IDEA_INPUT, MESSAGE_INPUT, CHOOSE_INPUT, ASK_INPUT = range(5)

config = get_config()

MODE_LABELS = {
    "check_idea": "💡 Проверить идею",
    "check_message": "💬 Проверить сообщение",
    "help_choose": "🤔 Помочь с выбором",
}

MODE_HINTS = {
    "check_idea": "Риски и слабые места",
    "check_message": "Как воспримут другие",
    "help_choose": "Сравню варианты",
}

ROUTE_LABELS = {
    "check_idea": "💡 Проверить идею",
    "check_message": "✉️ Проверить сообщение",
    "help_choose": "🤔 Помочь с выбором",
}


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu — heading, then 'Спросить' first, then specialized modes."""
    keyboard = [
        [
            InlineKeyboardButton(
                "❓ Спросить\nЗадайте любой вопрос или расскажите ситуацию.",
                callback_data="m:ask",
            )
        ],
        [
            InlineKeyboardButton(
                f"{MODE_LABELS['check_idea']}\n{MODE_HINTS['check_idea']}",
                callback_data="m:check_idea",
            )
        ],
        [
            InlineKeyboardButton(
                f"{MODE_LABELS['check_message']}\n{MODE_HINTS['check_message']}",
                callback_data="m:check_message",
            )
        ],
        [
            InlineKeyboardButton(
                f"{MODE_LABELS['help_choose']}\n{MODE_HINTS['help_choose']}",
                callback_data="m:help_choose",
            )
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("ℹ️ Как пользоваться", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")],
    ])


def ask_intro_keyboard() -> InlineKeyboardMarkup:
    """After an 'ask' answer — let user ask again or go to menu."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❓ Новый вопрос", callback_data="m:ask"),
            InlineKeyboardButton("⬅️ Главное меню", callback_data="menu"),
        ],
    ])


def route_keyboard(route_key: str) -> InlineKeyboardMarkup:
    """Offer to run the specialized mode on the same text, or just answer."""
    keyboard = [
        [
            InlineKeyboardButton(ROUTE_LABELS[route_key], callback_data=f"route:{route_key}"),
            InlineKeyboardButton("💬 Просто ответить", callback_data="menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — heading + problem-first question."""
    user = update.effective_user
    await get_or_create_user(user.id, user.username, user.first_name)

    text = (
        "🧠 **Второй мозг**\n\n"
        "Помогаю разобраться, проверить и принять решение.\n\n"
        "**Что нужно?**"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

    return SELECTING


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """How to use — simple."""
    text = (
        "📖 **Как пользоваться**\n\n"
        "**❓ Спросить** — напиши любой вопрос обычными словами. "
        "Если подойдёт специальная проверка, я предложу её.\n\n"
        "**💡 Проверить идею** — расскажи идею одним сообщением. "
        "Я найду риски и слабые места.\n\n"
        "**💬 Проверить сообщение** — вставь текст для отправки. "
        "Покажу, как его воспримут, и дам улучшенную версию.\n\n"
        "**🤔 Помочь с выбором** — напиши варианты через «или».\n\n"
        f"📊 Лимит: {config.daily_limit} запросов в сутки\n"
        f"📝 Макс. длина: {config.max_message_length} символов"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

    return SELECTING


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User statistics."""
    user = update.effective_user
    stats = await get_user_stats(user.id)

    text = (
        f"📊 **Твоя статистика**\n\n"
        f"👤 С нами с: {stats['first_seen'][:10] if stats['first_seen'] else '—'}\n"
        f"📝 Всего запросов: {stats['requests_total']}\n"
        f"📅 Сегодня: {stats['used_today']}/{config.daily_limit}\n"
        f"💬 Диалогов: {stats['total_conversations']}\n\n"
        f"Осталось сегодня: **{config.daily_limit - stats['used_today']}**"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")

    return SELECTING


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back to menu."""
    query = update.callback_query
    await query.answer()
    return await start(update, context)


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mode selection — friendly prompt with examples."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    mode_key = query.data.split(":")[1]

    if not await check_limit(user_id, config.daily_limit):
        remaining = await get_remaining(user_id, config.daily_limit)
        await query.edit_message_text(
            f"⛔ **Лимит на сегодня исчерпан**\n\n"
            f"Ты использовал все {config.daily_limit} запросов.\n"
            f"Возвращайся завтра! 🌅\n\n"
            f"Осталось: {remaining}",
            reply_markup=back_keyboard(),
            parse_mode="Markdown",
        )
        return SELECTING

    context.user_data["mode"] = mode_key

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
            "• «Стоит ли менять работу?»\n"
            "• «Запустить канал про ремонт?»"
        ),
        "check_message": (
            "💬 **Проверить сообщение**\n\n"
            "Вставь сообщение, которое хочешь отправить — покажу, как его воспримут.\n\n"
            "*Например:*\n"
            "• «Привет. Хотел уточнить про встречу»\n"
            "• «Спасибо за предложение, но я подумаю»"
        ),
        "help_choose": (
            "🤔 **Помочь с выбором**\n\n"
            "Напиши варианты, между которыми выбираешь.\n\n"
            "*Например:*\n"
            "• «Покупать эту машину или брать в лизинг?»\n"
            "• «Остаться в офисе или на удалёнке?»"
        ),
    }

    await query.edit_message_text(
        prompts[mode_key],
        reply_markup=back_keyboard(),
        parse_mode="Markdown",
    )

    state_map = {
        "ask": ASK_INPUT,
        "check_idea": IDEA_INPUT,
        "check_message": MESSAGE_INPUT,
        "help_choose": CHOOSE_INPUT,
    }
    return state_map[mode_key]


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose to run specialized mode on the text from 'Спросить'."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    target = query.data.split(":")[1]

    # Reuse saved text from the 'ask' step (no re-typing)
    saved_text = context.user_data.get("ask_text", "")
    if not saved_text:
        await query.edit_message_text(
            "Текст не сохранился. Выбери действие и напиши заново.",
            reply_markup=back_keyboard(),
        )
        return SELECTING

    # Show the same prompt the specialized mode would show, then process
    intro = {
        "check_idea": "💡 Отличная идея. Давай проверим.",
        "check_message": "💬 Посмотрим, как это воспримут.",
        "help_choose": "🤔 Разберём вместе.",
    }[target]

    # Simulate a message update with the saved text
    class _FakeMessage:
        def __init__(self, text):
            self.text = text

    class _FakeUpdate:
        def __init__(self, text):
            self.message = _FakeMessage(text)
            self.effective_user = query.from_user

    fake_update = _FakeUpdate(saved_text)
    return await _process_input(fake_update, context, target, intro)


async def handle_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _process_input(update, context, "check_idea", "💡 Отличная идея. Давай проверим.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _process_input(update, context, "check_message", "💬 Посмотрим, как это воспримут.")


async def handle_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _process_input(update, context, "help_choose", "🤔 Разберём вместе.")


async def handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Universal entry — answer, then offer routing if model suggests it."""
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if len(text) > config.max_message_length:
        await update.message.reply_text(
            f"⚠️ Слишком длинно. Максимум {config.max_message_length} символов.\n"
            f"У тебя: {len(text)}. Сократи и попробуй снова.",
            reply_markup=back_keyboard(),
        )
        return ASK_INPUT

    # Save original text for potential routing (p. 7)
    context.user_data["ask_text"] = text

    thinking = await update.message.reply_text("🧠 Думаю...")

    try:
        mode = get_mode("ask")
        history = await get_recent_conversations(user_id, config.history_limit)

        async with LLMClient() as client:
            response = await client.complete(
                system_prompt=mode.prompt,
                user_message=text,
                history=history,
            )

        await increment_usage(user_id)
        remaining = await get_remaining(user_id, config.daily_limit)

        # Parse routing marker from the LAST line
        content = response.content.rstrip()
        route_match = re.search(r"\[ROUTE:(\w+)\]\s*$", content)
        route_key = None
        if route_match and route_match.group(1) in ROUTE_LABELS:
            route_key = route_match.group(1)
            # Remove the marker line from what we show
            content = content[: route_match.start()].rstrip()

        await save_conversation(
            user_id=user_id,
            mode="ask",
            user_message=text,
            bot_response=content,
            tokens_used=response.tokens_used,
        )

        await thinking.delete()

        footer = f"\n\n—\n📊 Осталось сегодня: **{remaining}** запросов"

        if route_key:
            # Offer specialized mode on the same text
            await update.message.reply_text(
                f"{content}\n\n"
                "Похоже, здесь полезнее отдельная проверка.\nЧто выбрать?",
                reply_markup=route_keyboard(route_key),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"{content}{footer}",
                reply_markup=ask_intro_keyboard(),
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.exception(f"Error processing ask for user {user_id}")
        await thinking.delete()
        await update.message.reply_text(
            f"⚠️ Что-то пошло не так: {str(e)[:200]}\nПопробуй ещё раз или вернись в меню.",
            reply_markup=main_menu_keyboard(),
        )

    return SELECTING


async def _process_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode_key: str,
    intro: str,
) -> int:
    """Shared processing for specialized modes."""
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if len(text) > config.max_message_length:
        await update.message.reply_text(
            f"⚠️ Слишком длинно. Максимум {config.max_message_length} символов.\n"
            f"У тебя: {len(text)}. Сократи и попробуй снова.",
            reply_markup=back_keyboard(),
        )
        state_map = {"check_idea": IDEA_INPUT, "check_message": MESSAGE_INPUT, "help_choose": CHOOSE_INPUT}
        return state_map[mode_key]

    thinking = await update.message.reply_text(f"🧠 {intro}")

    try:
        mode = get_mode(mode_key)
        history = await get_recent_conversations(user_id, config.history_limit)

        async with LLMClient() as client:
            response = await client.complete(
                system_prompt=mode.prompt,
                user_message=text,
                history=history,
            )

        await increment_usage(user_id)
        remaining = await get_remaining(user_id, config.daily_limit)

        await save_conversation(
            user_id=user_id,
            mode=mode_key,
            user_message=text,
            bot_response=response.content,
            tokens_used=response.tokens_used,
        )

        await thinking.delete()

        await update.message.reply_text(
            f"{response.content}\n\n"
            f"—\n"
            f"📊 Осталось сегодня: **{remaining}** запросов",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception(f"Error processing {mode_key} for user {user_id}")
        await thinking.delete()
        await update.message.reply_text(
            f"⚠️ Что-то пошло не так: {str(e)[:200]}\nПопробуй ещё раз или вернись в меню.",
            reply_markup=main_menu_keyboard(),
        )

    return SELECTING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel."""
    await update.message.reply_text("Ок, вернулись в меню.", reply_markup=main_menu_keyboard())
    return SELECTING


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler."""
    logger.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Что-то пошло не так. Попробуй ещё раз или /start",
            reply_markup=main_menu_keyboard(),
        )


def get_conversation_handler() -> ConversationHandler:
    """Build main ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("stats", stats_command),
            CallbackQueryHandler(mode_callback, pattern=r"^m:"),
            CallbackQueryHandler(route_callback, pattern=r"^route:"),
            CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
            CallbackQueryHandler(help_command, pattern=r"^help$"),
            CallbackQueryHandler(stats_command, pattern=r"^stats$"),
        ],
        states={
            SELECTING: [
                CallbackQueryHandler(mode_callback, pattern=r"^m:"),
                CallbackQueryHandler(route_callback, pattern=r"^route:"),
                CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
                CallbackQueryHandler(help_command, pattern=r"^help$"),
                CallbackQueryHandler(stats_command, pattern=r"^stats$"),
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
