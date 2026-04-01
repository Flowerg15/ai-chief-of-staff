"""
Telegram inline keyboards for confirmations and actions.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def send_confirmation_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    """[Send] [Edit] [Skip] keyboard for email drafts."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Send", callback_data=f"send:{draft_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{draft_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{draft_id}"),
        ]
    ])


def confirm_keyboard(action: str, payload: str) -> InlineKeyboardMarkup:
    """Generic yes/no confirmation keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"{action}:confirm:{payload}"),
            InlineKeyboardButton("❌ No", callback_data=f"{action}:cancel:{payload}"),
        ]
    ])
