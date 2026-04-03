"""
Telegram bot setup and webhook registration.
Uses python-telegram-bot v21 in async mode.
"""
import structlog
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from app.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

_app: Application | None = None


def get_app() -> Application:
    global _app
    if _app is None:
        _app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )
        _register_handlers(_app)
    return _app


def _register_handlers(app: Application) -> None:
    from app.telegram.handlers import (
        handle_message,
        handle_callback_query,
        handle_start,
    )
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


async def setup_telegram_bot() -> None:
    """Register the webhook with Telegram."""
    app = get_app()
    await app.initialize()
    bot: Bot = app.bot
    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("Webhook registered", url=settings.webhook_url)


async def shutdown_telegram_bot() -> None:
    """Clean up the bot on shutdown."""
    app = get_app()
    await app.shutdown()


async def handle_telegram_update(body: dict) -> None:
    """Process an incoming Telegram update from the webhook."""
    app = get_app()
    update = Update.de_json(body, app.bot)

    # Security: only process messages from Garret's chat
    chat_id = None
    if update.message:
        chat_id = update.message.chat_id
    elif update.callback_query:
        chat_id = update.callback_query.message.chat_id

    if chat_id != settings.telegram_chat_id:
        logger.warning("Blocked update from unknown chat", chat_id=chat_id)
        return

    async with app:
        await app.process_update(update)


async def send_message(text: str, parse_mode: str = "Markdown", reply_markup=None) -> None:
    """Send a message to Garret's chat. Used by background jobs and proactive briefs."""
    app = get_app()
    bot: Bot = app.bot
    # Split long messages (Telegram limit is 4096 chars)
    chunks = _split_message(text)
    for i, chunk in enumerate(chunks):
        suffix = f"\n\n_[Part {i+1}/{len(chunks)}]_" if len(chunks) > 1 else ""
        # Only attach reply_markup to the last chunk
        markup = reply_markup if (i == len(chunks) - 1) else None
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=chunk + suffix,
            parse_mode=parse_mode,
            reply_markup=markup,
        )


def _split_message(text: str, max_len: int = 3800) -> list[str]:
    """Split a long message into chunks at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks
