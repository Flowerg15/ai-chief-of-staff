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
        elif intent == "compose":
            response = await _handle_compose(text, update)
        elif intent == "followup":
            response = await _handle_followup(text)
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
    reply_keywords = ["reply", "respond", "reply to"]
    compose_keywords = ["email", "send", "draft", "write", "forward", "compose", "message"]
    memory_keywords = ["remember", "update", "note that", "save that", "forget"]
    followup_keywords = ["follow up", "followup", "follow-up", "remind me", "ping me", "check back", "nudge"]

    if any(k in text_lower for k in summarise_keywords):
        return "summarise"
    if any(k in text_lower for k in followup_keywords):
        return "followup"
    # "reply to X" = reply to existing thread; "email X about Y" = compose new
    if any(k in text_lower for k in reply_keywords):
        return "draft"
    if any(k in text_lower for k in compose_keywords):
        # If there's an @ sign, it's likely a compose (new email to someone)
        if "@" in text_lower:
            return "compose"
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


async def _handle_compose(text: str, update: Update) -> str:
    """
    Compose and send a brand-new email (not a reply to an existing thread).
    Extracts recipient, subject, and generates body via Claude.
    """
    import uuid
    import re

    # Extract email address from the message
    email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
    if not email_match:
        return "I couldn't find an email address in your message. Try: 'Email john@example.com about ...'"

    to_email = email_match.group(0)

    # Look up contact for context
    client = get_supabase()
    contact_result = client.table("contacts").select("*").eq("email", to_email.lower()).maybe_single().execute()
    contact = contact_result.data if contact_result and contact_result.data else None
    to_name = contact["name"] if contact else to_email.split("@")[0].title()

    # Load tone samples
    category = "formal_external"
    if contact and contact.get("importance", 3) < 4:
        category = "quick_internal"
    tone_result = (
        client.table("tone_samples")
        .select("*")
        .eq("category", category)
        .eq("is_active", True)
        .limit(3)
        .execute()
    )
    tone_samples = tone_result.data or []

    tone_context = ""
    if tone_samples:
        tone_context = "\n\nTONE EXAMPLES (match this voice):\n"
        for i, sample in enumerate(tone_samples[:3], 1):
            tone_context += f"\n--- Example {i} ---\n{sample['body'][:400]}\n"

    contact_context = ""
    if contact:
        contact_context = f"\nContact: {contact['name']}"
        if contact.get("company"):
            contact_context += f" at {contact['company']}"
        if contact.get("notes"):
            contact_context += f"\nNotes: {contact['notes']}"

    prompt = (
        f"Garret wants to send a NEW email (not a reply).\n"
        f"Instruction: {text}\n"
        f"Recipient: {to_name} <{to_email}>\n"
        f"{contact_context}\n{tone_context}\n\n"
        "Generate TWO things, separated by the exact marker '---SUBJECT---':\n"
        "1. A short email subject line\n"
        "2. The email body\n\n"
        "Format:\n"
        "subject line here\n"
        "---SUBJECT---\n"
        "email body here\n\n"
        "Match Garret's tone. Keep it concise. No AI-sounding phrases."
    )

    raw = await ask_claude_complex(prompt, max_tokens=800)

    # Parse subject and body
    if "---SUBJECT---" in raw:
        parts = raw.split("---SUBJECT---", 1)
        subject = parts[0].strip()
        body = parts[1].strip()
    else:
        # Fallback: first line is subject
        lines = raw.strip().split("\n", 1)
        subject = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else raw.strip()

    # Clean up subject (remove "Subject:" prefix if Claude added it)
    subject = subject.removeprefix("Subject:").strip()

    draft_id = str(uuid.uuid4())[:8]
    draft = {
        "body": body,
        "to": to_email,
        "to_name": to_name,
        "subject": subject,
        "thread_id": None,  # New email, no thread
        "in_reply_to": None,
        "is_new": True,
    }
    _store_draft(draft_id, draft)

    preview = (
        f"*New email to {to_name}:*\n\n"
        f"Subject: _{subject}_\n\n"
        f"`{body}`"
    )

    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=send_confirmation_keyboard(draft_id),
    )
    return ""  # Already sent via reply_text


async def _handle_followup(text: str) -> str:
    """
    Parse a follow-up instruction like "Follow up with Doug in 3 days"
    and create a scheduled reminder in system_state.
    """
    from datetime import datetime, timezone, timedelta

    extraction_prompt = (
        f"The user wants to schedule a follow-up: '{text}'\n\n"
        "Extract as JSON with these fields:\n"
        '- "contact_name": the person to follow up with (or null)\n'
        '- "subject_hint": what the follow-up is about (short phrase)\n'
        '- "days": number of days from now to trigger the follow-up (integer)\n'
        '- "action": what to do when triggered — "check_reply" (see if they replied) or "nudge" (draft a nudge email)\n'
        '- "summary": one-line human-readable confirmation\n\n'
        "If the user says 'in a week', that's 7 days. 'Tomorrow' is 1. 'In a few days' is 3.\n"
        "Return ONLY valid JSON, no markdown fences."
    )

    raw = await ask_claude(extraction_prompt, max_tokens=300)

    try:
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except (json.JSONDecodeError, ValueError):
        return f"I understood the follow-up request but couldn't parse the timing. Try: 'Follow up with Doug in 3 days about the memo.'"

    days = data.get("days", 3)
    trigger_at = datetime.now(timezone.utc) + timedelta(days=days)

    followup = {
        "contact_name": data.get("contact_name"),
        "subject_hint": data.get("subject_hint", ""),
        "action": data.get("action", "check_reply"),
        "trigger_at": trigger_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    followup_key = f"followup:{trigger_at.strftime('%Y%m%d_%H%M%S')}"

    client = get_supabase()
    client.table("system_state").upsert({
        "key": followup_key,
        "value": followup,
    }).execute()

    trigger_display = trigger_at.strftime("%a, %b %d")
    summary = data.get("summary", f"Follow up with {data.get('contact_name', 'contact')} on {trigger_display}")

    return f"⏰ *Scheduled:* {summary}\n\nI'll check on *{trigger_display}* and ping you — with a draft nudge if they haven't replied."


async def _execute_send(draft_id: str, query) -> None:
    """Send the approved draft email — works for both replies and new emails."""
    draft = _load_draft(draft_id)
    if not draft:
        await query.edit_message_text("_Draft expired or already sent._", parse_mode="Markdown")
        return

    try:
        from datetime import datetime, timezone

        if draft.get("is_new") or not draft.get("thread_id"):
            # New email (compose)
            from app.gmail.client import send_new_email
            message_id = await send_new_email(
                to=draft["to"],
                subject=draft["subject"],
                body=draft["body"],
            )
        else:
            # Reply to existing thread
            from app.gmail.client import send_reply
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
