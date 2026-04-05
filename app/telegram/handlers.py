"""
Telegram message handlers.
Routes incoming messages to the appropriate workflow.
"""
import asyncio
import structlog
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.claude.client import ask_claude, ask_claude_complex
from app.claude.prompts import build_query_context
from app.database.client import get_supabase
from app.telegram.keyboards import send_confirmation_keyboard

logger = structlog.get_logger(__name__)

# In-memory draft store (keyed by draft_id)
# In V2, persist these in Supabase
_pending_drafts: dict[str, dict] = {}


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*AI Chief of Staff online.*\n\n"
        "Send me a message to:\n"
        "• Summarise your inbox\n"
        "• Draft a reply\n"
        "• Ask about a contact or deal\n"
        "• Process an attachment\n\n"
        "No commands needed — just talk.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route a message to the right workflow based on intent.
    Sends 'Processing...' immediately; edits when ready.
    """
    text = update.message.text.strip()
    if not text:
        return

    # Acknowledge immediately
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )
    processing_msg = await update.message.reply_text("_Processing..._", parse_mode="Markdown")

    try:
        intent = _classify_intent(text)

        # Guard: if intent looks like a draft confirmation but no drafts
        # are active, treat it as a general query instead of falsely
        # triggering "Draft has expired" on ambiguous short inputs.
        if intent == "draft" and not _pending_drafts:
            draft_action_words = {"send", "reply", "respond", "forward"}
            words = set(text.lower().split())
            if words.issubset(draft_action_words | {"it", "that", "the", "this", "ok", "yes", "please", "now", "to", "a"}):
                intent = "query"

        logger.info("Message received", intent=intent, preview=text[:80])

        if intent == "summarise":
            response = await _handle_summarise(text)
        elif intent == "draft":
            response = await _handle_draft(text, update)
        elif intent == "query":
            response = await _handle_query(text)
        elif intent == "memory":
            response = await _handle_memory_update(text)
        else:
            response = await ask_claude(text)

        if response:
            await processing_msg.edit_text(response, parse_mode="Markdown")
        else:
            await processing_msg.delete()

    except Exception as e:
        logger.error("Handler error", error=str(e), intent=text[:60])
        await processing_msg.edit_text(
            f"⚠️ Something went wrong: `{str(e)[:200]}`\n\nTry again or check /health.",
            parse_mode="Markdown",
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    logger.info("Callback query", data=data)

    if data.startswith("send:"):
        draft_id = data.split(":", 1)[1]
        await _execute_send(draft_id, query)

    elif data.startswith("edit:"):
        draft_id = data.split(":", 1)[1]
        draft = _pending_drafts.get(draft_id)
        if draft:
            await query.edit_message_text(
                f"*Edit the draft and send it back to me:*\n\n`{draft['body']}`",
                parse_mode="Markdown",
            )

    elif data.startswith("skip:"):
        draft_id = data.split(":", 1)[1]
        _pending_drafts.pop(draft_id, None)
        await query.edit_message_text("_Skipped._", parse_mode="Markdown")


def _classify_intent(text: str) -> str:
    """
    Simple keyword-based intent classification.
    Claude handles the actual NLU; this is just routing.
    """
    text_lower = text.lower()

    summarise_keywords = ["summarise", "summarize", "inbox", "what's new", "what's in", "check email", "brief", "what came in"]
    draft_keywords = ["reply", "draft", "write", "respond", "send", "forward"]
    memory_keywords = ["remember", "update", "note that", "save that", "forget"]

    if any(k in text_lower for k in summarise_keywords):
        return "summarise"
    if any(k in text_lower for k in draft_keywords):
        return "draft"
    if any(k in text_lower for k in memory_keywords):
        return "memory"
    return "query"


async def _handle_summarise(text: str) -> str:
    """Pull recent emails and return a structured summary."""
    from app.workflows.inbox import summarise_inbox
    return await summarise_inbox()


async def _handle_query(text: str) -> str:
    """Answer a question about a contact, deal, or thread."""
    from app.workflows.inbox import retrieve_context_for_query
    context = await retrieve_context_for_query(text)
    return await ask_claude(text, context=context)


async def _handle_draft(text: str, update: Update) -> str:
    """
    Draft a reply and present it with Send/Edit/Skip keyboard.
    """
    from app.workflows.draft import draft_reply
    import uuid

    result = await draft_reply(text)
    if not result:
        return "Couldn't find the thread to draft a reply for. Can you be more specific?"

    draft_id = str(uuid.uuid4())[:8]
    _pending_drafts[draft_id] = result

    preview = (
        f"*Draft reply to {result.get('to_name', result['to'])}:*\n\n"
        f"`{result['body']}`\n\n"
        f"Subject: _{result['subject']}_"
    )

    # Send with confirmation keyboard
    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=send_confirmation_keyboard(draft_id),
    )
    return ""  # Main response already sent via reply_markup


async def _handle_memory_update(text: str) -> str:
    """Parse a memory update instruction and apply it to the database."""
    import json

    prompt = (
        f"The user wants to update memory: '{text}'\n\n"
        "Extract the update as JSON with keys: table (contacts|deals|notes), "
        "identifier (name or email to match), and fields (dict of columns to set).\n"
        "Also include a confirmation message in a 'message' key.\n"
        "Return ONLY valid JSON, no markdown."
    )
    raw = await ask_claude(prompt)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return await ask_claude(
            f"The user wants to update memory: '{text}'\n\n"
            "Identify what needs to be updated (contact, deal, or note) and confirm what you're saving."
        )

    table = parsed.get("table", "contacts")
    identifier = parsed.get("identifier", "")
    fields = parsed.get("fields", {})
    confirmation = parsed.get("message", "Memory updated.")

    if not identifier or not fields:
        return confirmation

    client = get_supabase()
    if table == "contacts":
        client.table("contacts").update(fields).eq("email", identifier).execute()
    elif table == "deals":
        client.table("deals").update(fields).eq("name", identifier).execute()
    elif table == "notes":
        from datetime import datetime, timezone
        client.table("decisions").insert({
            "context": identifier,
            "decision": json.dumps(fields),
            "rationale": text,
            "date": datetime.now(timezone.utc).isoformat(),
        }).execute()

    return f"✅ {confirmation}"


async def _execute_send(draft_id: str, query) -> None:
    """Send the approved draft email."""
    draft = _pending_drafts.pop(draft_id, None)
    if not draft:
        await query.edit_message_text("_Draft expired or already sent._", parse_mode="Markdown")
        return

    try:
        from app.gmail.client import send_reply
        from app.database.client import get_supabase
        from datetime import datetime, timezone

        message_id = await send_reply(
            thread_id=draft["thread_id"],
            message_id=draft["in_reply_to"],
            to=draft["to"],
            subject=draft["subject"],
            body=draft["body"],
        )

        # Audit log
        client = get_supabase()
        client.table("audit_log").insert({
            "action": "email_sent",
            "gmail_message_id": message_id,
            "recipient": draft["to"],
            "subject": draft["subject"],
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        await query.edit_message_text(
            f"✅ *Sent* to {draft.get('to_name', draft['to'])}.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Email send failed", error=str(e))
        await query.edit_message_text(
            f"❌ Failed to send: `{str(e)[:200]}`",
            parse_mode="Markdown",
        )
