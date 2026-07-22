"""
Telegram bot handlers with FSM for 3 modes:
- challenge (Оспорить)
- message_check (Проверить сообщение)
- choose (Помочь выбрать)
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
)
from llm_client import LLMClient, Mode
from prompts import get_mode, list_modes

logger = logging.getLogger(__name__)

# Conversation states
SELECTING_MODE, CHALLENGE_INPUT, MESSAGE_CHECK_INPUT, CHOOSE_INPUT = range(4)

config = get_config()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu inline keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("💥 Оспорить идею", callback_data="mode:challenge"),
            InlineKeyboardButton("✉️ Проверить сообщение", callback_data="mode:message_check"),
        ],
        [
            InlineKeyboardButton("⚖️ Помочь выбрать", callback_data="mode:choose"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_keyboard() -> InlineKeyboardMarkup:
    """Back to menu keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Назад в меню", callback_data="menu")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point - show main menu."""
    user = update.effective_user
    await get_or_create_user(user.id, user.username, user.first_name)
    
    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я — твой **Второй мозг**. Помогаю быстро принимать решения:\n\n"
        "💥 **Оспорить** — найду риски и слабые места в твоей идее\n"
        "✉️ **Проверить** — проанализирую тон, стиль, грамматику сообщения\n"
        "⚖️ **Выбрать** — сравню варианты и дам обоснованную рекомендацию\n\n"
        "Выбери режим:"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    
    return SELECTING_MODE


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show help."""
    text = (
        "📖 **Как пользоваться**\n\n"
        "**💥 Оспорить идею** — опиши идею или план одним сообщением. "
        "Бот выступит в роли дьявола-адвоката: найдёт риски, слабые места, "
        "предложит как проверить гипотезу дёшево.\n\n"
        "**✉️ Проверить сообщение** — пришли текст, который хочешь отправить. "
        "Бот проанализирует тон, найдёт резкие места, ошибки, и даст улучшенную версию.\n\n"
        "**⚖️ Помочь выбрать** — опиши ситуацию с вариантами через «или». "
        "Например: *«Нанять джуниора за 80к или сеньора за 180к?»* "
        "Бот сравнит по критериям и даст рекомендацию.\n\n"
        f"📊 Лимит: {config.daily_limit} запросов в сутки\n"
        f"📝 Макс. длина сообщения: {config.max_message_length} символов"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    
    return SELECTING_MODE


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show user statistics."""
    from db import get_user_stats
    user = update.effective_user
    stats = await get_user_stats(user.id)
    
    text = (
        f"📊 **Твоя статистика**\n\n"
        f"👤 Зарегистрирован: {stats['first_seen'][:10] if stats['first_seen'] else '—'}\n"
        f"📝 Всего запросов: {stats['requests_total']}\n"
        f"📅 Сегодня использовано: {stats['used_today']}/{config.daily_limit}\n"
        f"💬 Всего диалогов: {stats['total_conversations']}\n\n"
        f"Осталось сегодня: **{config.daily_limit - stats['used_today']}**"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
    
    return SELECTING_MODE


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle 'back to menu' callback."""
    query = update.callback_query
    await query.answer()
    return await start(update, context)


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle mode selection."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    mode_key = query.data.split(":")[1]
    
    # Check daily limit
    if not await check_limit(user_id, config.daily_limit):
        remaining = await get_remaining(user_id, config.daily_limit)
        await query.edit_message_text(
            f"⛔ **Лимит исчерпан**\n\n"
            f"Ты использовал все {config.daily_limit} запросов на сегодня.\n"
            f"Попробуй завтра! 🌅\n\n"
            f"Осталось: {remaining}",
            reply_markup=back_keyboard(),
            parse_mode="Markdown"
        )
        return SELECTING_MODE
    
    # Store mode in context
    context.user_data["mode"] = mode_key
    
    # Get mode info
    mode = get_mode(mode_key)
    
    prompt_text = {
        "challenge": (
            "💥 **Оспорить идею**\n\n"
            "Опиши свою идею, план или гипотезу одним сообщением.\n"
            "Я найду риски, слабые места и скажу, как проверить дёшево.\n\n"
            "*Пример: «Хочу открыть кофейню в спальном районе — только тейк-ауф, никакого зала»*"
        ),
        "message_check": (
            "✉️ **Проверить сообщение**\n\n"
            "Пришли текст, который хочешь проверить перед отправкой.\n"
            "Я проанализирую тон, стиль, грамматику и дам улучшенную версию.\n\n"
            "*Пример: «Иван, вы опять опоздали. Если повторится — уволю.»*"
        ),
        "choose": (
            "⚖️ **Помочь выбрать**\n\n"
            "Опиши ситуацию и варианты через «или».\n"
            "Я выделю критерии, сравню плюсы/минусы и дам рекомендацию.\n\n"
            "*Пример: «Нанять джуниора за 80к и учить, или сеньора за 180к — сразу в дело?»*"
        ),
    }
    
    await query.edit_message_text(
        prompt_text[mode_key],
        reply_markup=back_keyboard(),
        parse_mode="Markdown"
    )
    
    # Return appropriate state
    state_map = {
        "challenge": CHALLENGE_INPUT,
        "message_check": MESSAGE_CHECK_INPUT,
        "choose": CHOOSE_INPUT,
    }
    return state_map[mode_key]


async def handle_challenge_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user input for challenge mode."""
    return await _process_mode_input(update, context, "challenge")


async def handle_message_check_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user input for message check mode."""
    return await _process_mode_input(update, context, "message_check")


async def handle_choose_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user input for choose mode."""
    return await _process_mode_input(update, context, "choose")


async def _process_mode_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode_key: str
) -> int:
    """Process user input for a specific mode."""
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()
    
    # Validate length
    if len(text) > config.max_message_length:
        await update.message.reply_text(
            f"⚠️ Слишком длинное сообщение. Максимум {config.max_message_length} символов.\n"
            f"У тебя: {len(text)}. Сократи и попробуй снова.",
            reply_markup=back_keyboard()
        )
        # Stay in same state
        state_map = {
            "challenge": CHALLENGE_INPUT,
            "message_check": MESSAGE_CHECK_INPUT,
            "choose": CHOOSE_INPUT,
        }
        return state_map[mode_key]
    
    # Show thinking indicator
    thinking = await update.message.reply_text("🧠 Думаю...")
    
    try:
        # Get mode and system prompt
        mode = get_mode(mode_key)
        system_prompt = mode.prompt
        
        # Get conversation history for context
        history = await get_recent_conversations(user_id, config.history_limit)
        
        # Call LLM
        async with LLMClient() as client:
            response = await client.complete(
                system_prompt=system_prompt,
                user_message=text,
                history=history,
            )
        
        # Save usage
        await increment_usage(user_id)
        remaining = await get_remaining(user_id, config.daily_limit)
        
        # Save conversation
        await save_conversation(
            user_id=user_id,
            mode=mode_key,
            user_message=text,
            bot_response=response.content,
            tokens_used=response.tokens_used,
        )
        
        # Delete thinking message
        await thinking.delete()
        
        # Send response
        await update.message.reply_text(
            f"{response.content}\n\n"
            f"—\n"
            f"📊 Осталось сегодня: **{remaining}** запросов\n"
            f"🔢 Токенов использовано: {response.tokens_used}",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.exception(f"Error processing {mode_key} for user {user_id}")
        await thinking.delete()
        await update.message.reply_text(
            f"⚠️ Произошла ошибка: {str(e)[:200]}\n"
            f"Попробуй ещё раз или выбери другой режим.",
            reply_markup=main_menu_keyboard()
        )
    
    return SELECTING_MODE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel current operation."""
    await update.message.reply_text(
        "Отменено. Выбери режим:",
        reply_markup=main_menu_keyboard()
    )
    return SELECTING_MODE


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler."""
    logger.exception("Unhandled error", exc_info=context.error)
    
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Произошла непредвиденная ошибка. Попробуй ещё раз или /start",
            reply_markup=main_menu_keyboard()
        )


def get_conversation_handler() -> ConversationHandler:
    """Build and return the main ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("stats", stats_command),
            CallbackQueryHandler(mode_callback, pattern=r"^mode:"),
            CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
            CallbackQueryHandler(help_command, pattern=r"^help$"),
            CallbackQueryHandler(stats_command, pattern=r"^stats$"),
        ],
        states={
            SELECTING_MODE: [
                CallbackQueryHandler(mode_callback, pattern=r"^mode:"),
                CallbackQueryHandler(menu_callback, pattern=r"^menu$"),
                CallbackQueryHandler(help_command, pattern=r"^help$"),
                CallbackQueryHandler(stats_command, pattern=r"^stats$"),
            ],
            CHALLENGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_challenge_input),
                CommandHandler("cancel", cancel),
            ],
            MESSAGE_CHECK_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_check_input),
                CommandHandler("cancel", cancel),
            ],
            CHOOSE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choose_input),
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