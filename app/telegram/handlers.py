"""
Telegram message handlers.
Routes incoming messages to the appropriate workflow.
"""
import asyncio
import json
import structlog
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.claude.client import ask_claude, ask_claude_complex
from app.claude.prompts import build_query_context
from app.database.client import get_supabase
from app.telegram.keyboards import send_confirmation_keyboard

logger = structlog.get_logger(__name__)


# ─── Draft persistence (Supabase-backed) ────────────────────────────────────

def _store_draft(draft_id: str, draft: dict) -> None:
    """Persist a pending draft to Supabase system_state."""
    client = get_supabase()
    client.table("system_state").upsert({
        "key": f"draft:{draft_id}",
        "value": draft,
    }).execute()


def _load_draft(draft_id: str) -> dict | None:
    """Load a pending draft from Supabase. Returns None if expired/missing."""
    client = get_supabase()
    result = client.table("system_state").select("value").eq("key", f"draft:{draft_id}").maybe_single().execute()
    if result.data:
        _delete_draft(draft_id)
        return result.data["value"]
    return None


def _delete_draft(draft_id: str) -> None:
    """Remove a draft from Supabase."""
    try:
        client = get_supabase()
        client.table("system_state").delete().eq("key", f"draft:{draft_id}").execute()
    except Exception:
        pass


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

        await processing_msg.edit_text(response, parse_mode="Markdown")

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
        draft = _load_draft(draft_id)
        if draft:
            # Re-store so user can still send after editing
            _store_draft(draft_id, draft)
            await query.edit_message_text(
                f"*Edit the draft and send it back to me:*\n\n`{draft['body']}`",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("_Draft expired._", parse_mode="Markdown")

    elif data.startswith("skip:"):
        draft_id = data.split(":", 1)[1]
        _delete_draft(draft_id)
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
    _store_draft(draft_id, result)

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
    """
    Parse a memory update instruction, extract structured data via Claude,
    and persist it to Supabase.
    """
    from datetime import datetime, timezone

    extraction_prompt = (
        f"The user wants to update their memory/CRM: '{text}'\n\n"
        "Extract the update as JSON with these fields:\n"
        '- "type": one of "contact", "deal", or "note"\n'
        '- "name": the person or deal name\n'
        '- "updates": a dict of fields to update (e.g., {{"title": "VP of Sales", "company": "Acme"}})\n'
        '- "summary": a one-line human-readable summary of what was saved\n\n'
        "Return ONLY valid JSON, no markdown fences."
    )

    raw = await ask_claude(extraction_prompt, max_tokens=300)

    try:
        import json
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except (json.JSONDecodeError, ValueError):
        return f"I understood your request but couldn't parse it into a structured update. Here's what I got:\n\n{raw}"

    client = get_supabase()
    record_type = data.get("type", "note")
    name = data.get("name", "")
    updates = data.get("updates", {})
    summary = data.get("summary", "Update saved.")

    if record_type == "contact" and name:
        # Upsert contact
        existing = client.table("contacts").select("*").ilike("name", name).maybe_single().execute()
        if existing.data:
            client.table("contacts").update(updates).eq("id", existing.data["id"]).execute()
        else:
            client.table("contacts").insert({"name": name, **updates}).execute()

    elif record_type == "deal" and name:
        existing = client.table("deals").select("*").ilike("name", name).maybe_single().execute()
        if existing.data:
            client.table("deals").update(updates).eq("id", existing.data["id"]).execute()
        else:
            client.table("deals").insert({"name": name, **updates}).execute()

    else:
        # Store as a note in system_state
        note_key = f"note:{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        client.table("system_state").upsert({
            "key": note_key,
            "value": {"text": text, "parsed": data},
        }).execute()

    return f"✅ *Saved:* {summary}"


async def _execute_send(draft_id: str, query) -> None:
    """Send the approved draft email."""
    draft = _load_draft(draft_id)
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
