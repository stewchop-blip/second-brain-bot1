"""
Telegram bot handlers.
Rewritten UX: problem-first language, no "режим" word, no "AI" word,
examples, friendly intros, quick-action buttons after each answer.
"""
import logging
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
    get_user_stats,
)
from llm_client import LLMClient
from prompts import get_mode

logger = logging.getLogger(__name__)

# Conversation states
SELECTING, IDEA_INPUT, MESSAGE_INPUT, CHOOSE_INPUT = range(4)

config = get_config()

MODE_LABELS = {
    "check_idea": "💡 Проверить идею",
    "check_message": "💬 Проверить сообщение",
    "help_choose": "🤔 Помочь с выбором",
}

MODE_HINTS = {
    "check_idea": "Найду слабые места, риски и то, что можно упустить.",
    "check_message": "Покажу, как ваше сообщение может воспринять другой человек.",
    "help_choose": "Помогу сравнить варианты и принять решение.",
}


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu — problem-first, with descriptions."""
    keyboard = [
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


def after_answer_keyboard() -> InlineKeyboardMarkup:
    """Quick actions after each answer."""
    keyboard = [
        [
            InlineKeyboardButton("🔄 Проверить ещё", callback_data="menu"),
            InlineKeyboardButton("✏️ Исправить ответ", callback_data="menu"),
        ],
        [
            InlineKeyboardButton("⬅️ Главное меню", callback_data="menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — clear, problem-first welcome."""
    user = update.effective_user
    await get_or_create_user(user.id, user.username, user.first_name)

    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я — «Второй мозг». Помогаю перед важными шагами.\n\n"
        "За минуту могу:\n"
        "• проверить идею;\n"
        "• проверить сообщение перед отправкой;\n"
        "• помочь выбрать между вариантами.\n\n"
        "Чем могу помочь?"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())

    return SELECTING


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """How to use — simple."""
    text = (
        "📖 **Как пользоваться**\n\n"
        "**💡 Проверить идею** — расскажи свою идею одним сообщением. "
        "Я найду риски, слабые места и скажу, как проверить дёшево.\n\n"
        "**💬 Проверить сообщение** — вставь текст, который хочешь отправить. "
        "Я покажу, как его воспримет другой человек, и дам улучшенную версию.\n\n"
        "**🤔 Помочь с выбором** — напиши варианты через «или». "
        "Например: *«Нанять джуниора или сеньора?»* "
        "Я сравню и дам совет.\n\n"
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
        "check_idea": IDEA_INPUT,
        "check_message": MESSAGE_INPUT,
        "help_choose": CHOOSE_INPUT,
    }
    return state_map[mode_key]


async def handle_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _process_input(update, context, "check_idea", "💡 Отличная идея. Давай проверим.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _process_input(update, context, "check_message", "💬 Посмотрим, как это воспримут.")


async def handle_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _process_input(update, context, "help_choose", "🤔 Разберём вместе.")


async def _process_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode_key: str,
    intro: str,
) -> int:
    """Shared processing for all three modes."""
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
            reply_markup=after_answer_keyboard(),
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
            CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
            CallbackQueryHandler(help_command, pattern=r"^help$"),
            CallbackQueryHandler(stats_command, pattern=r"^stats$"),
        ],
        states={
            SELECTING: [
                CallbackQueryHandler(mode_callback, pattern=r"^m:"),
                CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
                CallbackQueryHandler(help_command, pattern=r"^help$"),
                CallbackQueryHandler(stats_command, pattern=r"^stats$"),
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
