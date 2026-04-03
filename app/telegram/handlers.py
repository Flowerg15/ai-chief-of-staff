"""
Telegram message handlers.
Routes incoming messages to the appropriate workflow.
Maintains full conversation history in Supabase for multi-turn context.
"""
import asyncio
import json
import structlog
from datetime import datetime, timezone, timedelta
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
    # Also store as "last_draft" for "send that" continuity
    client.table("system_state").upsert({
        "key": "last_draft_id",
        "value": {"draft_id": draft_id},
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


def _get_last_draft_id() -> str | None:
    """Get the most recently created draft ID."""
    try:
        client = get_supabase()
        result = client.table("system_state").select("value").eq("key", "last_draft_id").maybe_single().execute()
        if result.data:
            return result.data["value"].get("draft_id")
    except Exception:
        pass
    return None


# ─── Conversation history (Supabase-backed) ─────────────────────────────────

def _save_message(role: str, content: str) -> None:
    """Save a message to conversation history."""
    from datetime import datetime, timezone
    try:
        client = get_supabase()
        ts = datetime.now(timezone.utc).isoformat()
        key = f"chat:{ts}:{role}"
        client.table("system_state").upsert({
            "key": key,
            "value": {
                "role": role,
                "content": content[:3000],  # Cap per-message to keep context manageable
                "timestamp": ts,
            },
        }).execute()
    except Exception as e:
        logger.debug("Failed to save chat message", error=str(e))


def _load_conversation_history(limit: int = 20) -> list[dict]:
    """
    Load recent conversation history from Supabase.
    Returns list of {"role": "user"|"assistant", "content": "..."} dicts,
    ordered oldest to newest.
    """
    try:
        client = get_supabase()
        result = (
            client.table("system_state")
            .select("value")
            .like("key", "chat:%")
            .order("key", desc=True)
            .limit(limit)
            .execute()
        )
        messages = []
        for row in reversed(result.data or []):
            val = row.get("value", {})
            messages.append({
                "role": val.get("role", "user"),
                "content": val.get("content", ""),
            })
        return messages
    except Exception as e:
        logger.debug("Failed to load chat history", error=str(e))
        return []


def _format_history_for_context(history: list[dict]) -> str:
    """Format conversation history as a context block for Claude."""
    if not history:
        return ""

    lines = ["=== CONVERSATION HISTORY ===\n"]
    for msg in history:
        role_label = "Garret" if msg["role"] == "user" else "Chief of Staff"
        lines.append(f"{role_label}: {msg['content'][:500]}")
    lines.append("\n=== END HISTORY ===\n")
    return "\n".join(lines)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*AI Chief of Staff online.*\n\n"
        "Send me a message to:\n"
        "• Summarise your inbox\n"
        "• Draft a reply or compose a new email\n"
        "• Ask about a contact or deal\n"
        "• Set a follow-up reminder\n\n"
        "No commands needed — just talk.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route a message to the right workflow based on intent.
    Saves all messages to conversation history for multi-turn context.
    """
    text = update.message.text.strip()
    if not text:
        return

    # Save user message to history
    _save_message("user", text)

    # Acknowledge immediately
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )
    processing_msg = await update.message.reply_text("_Processing..._", parse_mode="Markdown")

    try:
        intent = _classify_intent(text)
        logger.info("Message received", intent=intent, preview=text[:80])

        if intent == "send_last":
            response = await _handle_send_last(text, update)
        elif intent == "edit_last":
            response = await _handle_edit_last(text, update)
        elif intent == "calendar":
            response = await _handle_calendar(text)
        elif intent == "summarise":
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
            # General chat — include conversation history for context
            history = _load_conversation_history(limit=20)
            history_context = _format_history_for_context(history)
            response = await ask_claude(text, context=history_context)

        # Save assistant response to history (if non-empty)
        if response:
            _save_message("assistant", response)
            await processing_msg.edit_text(response, parse_mode="Markdown")
        else:
            # Response was sent directly (e.g., draft with keyboard)
            try:
                await processing_msg.delete()
            except Exception:
                await processing_msg.edit_text("👆", parse_mode="Markdown")

    except Exception as e:
        logger.error("Handler error", error=str(e), intent=text[:60])
        error_msg = f"⚠️ Something went wrong: `{str(e)[:200]}`\n\nTry again or check /health."
        _save_message("assistant", error_msg)
        await processing_msg.edit_text(error_msg, parse_mode="Markdown")


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
    Keyword-based intent classification with conversation-aware fallbacks.
    """
    text_lower = text.lower().strip()

    # Short confirmations that refer to last draft
    send_now_phrases = [
        "send", "send it", "send that", "yes send", "send that email",
        "go ahead", "looks good send it", "yes", "ship it", "fire it off",
        "send the email", "new message send that email",
    ]
    if text_lower in send_now_phrases or (len(text_lower) < 30 and "send" in text_lower):
        # Check if there's a pending draft to send
        last_id = _get_last_draft_id()
        if last_id:
            return "send_last"

    summarise_keywords = ["summarise", "summarize", "inbox", "what's new", "what's in", "check email", "brief", "what came in"]
    reply_keywords = ["reply", "respond", "reply to"]
    compose_keywords = ["email", "draft", "write", "forward", "compose"]
    memory_keywords = ["remember", "update", "note that", "save that", "forget"]
    followup_keywords = ["follow up", "followup", "follow-up", "remind me", "ping me", "check back", "nudge"]
    calendar_keywords = ["schedule", "calendar", "what's on my calendar", "my day", "meetings today", "book a meeting", "set up a call", "create event", "block time", "what do i have"]

    # Edit-last-draft detection
    edit_phrases = ["make it shorter", "make it longer", "more casual", "more formal",
                    "change the tone", "rewrite", "try again", "too long", "too short",
                    "tweak it", "adjust"]
    if any(p in text_lower for p in edit_phrases):
        last_id = _get_last_draft_id()
        if last_id:
            return "edit_last"

    if any(k in text_lower for k in summarise_keywords):
        return "summarise"
    if any(k in text_lower for k in calendar_keywords):
        return "calendar"
    if any(k in text_lower for k in followup_keywords):
        return "followup"
    if any(k in text_lower for k in reply_keywords):
        return "draft"
    if any(k in text_lower for k in compose_keywords):
        if "@" in text_lower:
            return "compose"
        return "draft"
    if any(k in text_lower for k in memory_keywords):
        return "memory"
    return "query"


async def _handle_calendar(text: str) -> str:
    """Handle calendar queries — show today's events, upcoming, or create new events."""
    text_lower = text.lower()

    # Check if this is a "create event" request
    create_keywords = ["book", "set up", "create", "block", "schedule a", "add to calendar"]
    is_create = any(k in text_lower for k in create_keywords)

    if is_create:
        return await _handle_create_event(text)

    # Otherwise, show calendar
    from app.calendar.client import get_todays_events, get_upcoming_events, format_events_for_context

    try:
        if "tomorrow" in text_lower:
            from datetime import timedelta
            from zoneinfo import ZoneInfo
            from app.config import get_settings
            from app.calendar.client import list_events
            settings = get_settings()
            tz = ZoneInfo(settings.timezone)
            tomorrow = datetime.now(tz) + timedelta(days=1)
            start = tomorrow.replace(hour=0, minute=0, second=0)
            end = tomorrow.replace(hour=23, minute=59, second=59)
            events = await list_events(start, end)
            day_label = "Tomorrow"
        elif "week" in text_lower:
            from datetime import timedelta
            from zoneinfo import ZoneInfo
            from app.config import get_settings
            from app.calendar.client import list_events
            settings = get_settings()
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(tz)
            end = now + timedelta(days=7)
            events = await list_events(now, end)
            day_label = "This week"
        else:
            events = await get_todays_events()
            day_label = "Today"

        if not events:
            return f"📅 *{day_label}:* Nothing on the calendar. Your day is clear."

        formatted = format_events_for_context(events)

        # Get conversation history for context
        history = _load_conversation_history(limit=5)
        history_context = _format_history_for_context(history)

        prompt = (
            f"Garret asked about his calendar. Here's what's on it:\n\n"
            f"=== {day_label.upper()}'S CALENDAR ===\n{formatted}\n\n"
            f"{history_context}\n\n"
            "Give a brief, useful summary of the day. Cross-reference with any "
            "contacts or deals you know about. Mention any prep needed for meetings "
            "with external parties. Keep it under 500 chars. Be direct."
        )

        return await ask_claude(prompt, max_tokens=500)

    except Exception as e:
        logger.error("Calendar handler failed", error=str(e))
        return f"⚠️ Couldn't load calendar: `{str(e)[:200]}`"


async def _handle_create_event(text: str) -> str:
    """Parse a natural language event request and create a calendar event."""
    from app.calendar.client import create_event
    from zoneinfo import ZoneInfo
    from app.config import get_settings

    settings = get_settings()

    extraction_prompt = (
        f"The user wants to create a calendar event: '{text}'\n\n"
        f"Today is {datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}.\n"
        f"User's timezone: {settings.timezone}\n\n"
        "Extract as JSON with these fields:\n"
        '- "summary": event title (short)\n'
        '- "date": date in YYYY-MM-DD format\n'
        '- "start_hour": start hour (24h format, integer)\n'
        '- "start_minute": start minute (integer, default 0)\n'
        '- "duration_minutes": duration in minutes (default 60)\n'
        '- "attendees": list of email addresses (or empty list)\n'
        '- "location": location string (or empty string)\n'
        '- "description": brief description (or empty string)\n\n'
        "If the user says 'tomorrow', compute the actual date. "
        "If they say 'this Friday', compute it. "
        "If no time is specified, default to 10:00 AM.\n"
        "Return ONLY valid JSON, no markdown fences."
    )

    raw = await ask_claude(extraction_prompt, max_tokens=300)

    try:
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except (json.JSONDecodeError, ValueError):
        return "I couldn't parse the event details. Try: 'Schedule a call with Doug tomorrow at 2pm'"

    try:
        tz = ZoneInfo(settings.timezone)
        date_parts = data["date"].split("-")
        start_time = datetime(
            int(date_parts[0]), int(date_parts[1]), int(date_parts[2]),
            data.get("start_hour", 10), data.get("start_minute", 0),
            tzinfo=tz,
        )
        end_time = start_time + timedelta(minutes=data.get("duration_minutes", 60))

        result = await create_event(
            summary=data.get("summary", "Meeting"),
            start_time=start_time,
            end_time=end_time,
            description=data.get("description", ""),
            attendees=data.get("attendees", []) or None,
            location=data.get("location", ""),
        )

        time_display = start_time.strftime("%-I:%M%p").lower()
        date_display = start_time.strftime("%A, %b %d")

        meet_link = result.get("hangoutLink", "")
        meet_line = f"\n*Meet:* {meet_link}" if meet_link else ""
        attendees_line = f"\n*With:* {', '.join(data.get('attendees', []))}" if data.get('attendees') else ""

        return (
            f"📅 *Created:* {data.get('summary', 'Meeting')}\n"
            f"*When:* {date_display} at {time_display}"
            f"{attendees_line}"
            f"{meet_line}"
        )

    except Exception as e:
        logger.error("Event creation failed", error=str(e))
        return f"⚠️ Couldn't create event: `{str(e)[:200]}`"


async def _handle_summarise(text: str) -> str:
    """Pull recent emails and return a structured summary."""
    from app.workflows.inbox import summarise_inbox
    return await summarise_inbox()


async def _handle_query(text: str) -> str:
    """Answer a question about a contact, deal, or thread — with conversation history."""
    from app.workflows.inbox import retrieve_context_for_query
    db_context = await retrieve_context_for_query(text)

    # Add conversation history
    history = _load_conversation_history(limit=20)
    history_context = _format_history_for_context(history)
    full_context = f"{history_context}\n\n{db_context}" if history_context else db_context

    return await ask_claude(text, context=full_context)


async def _handle_draft(text: str, update: Update) -> str:
    """Draft a reply and present it with Send/Edit/Skip keyboard."""
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

    _save_message("assistant", f"[Drafted reply to {result.get('to_name', result['to'])}] {result['body'][:200]}")

    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=send_confirmation_keyboard(draft_id),
    )
    return ""


async def _handle_memory_update(text: str) -> str:
    """Parse a memory update, extract structured data, persist to Supabase."""
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
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except (json.JSONDecodeError, ValueError):
        return f"I understood your request but couldn't parse it into a structured update. Here's what I got:\n\n{raw}"

    client = get_supabase()
    record_type = data.get("type", "note")
    name = data.get("name", "")
    updates = data.get("updates", {})
    summary = data.get("summary", "Update saved.")

    if record_type == "contact" and name:
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
        note_key = f"note:{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        client.table("system_state").upsert({
            "key": note_key,
            "value": {"text": text, "parsed": data},
        }).execute()

    return f"✅ *Saved:* {summary}"


async def _handle_compose(text: str, update: Update) -> str:
    """Compose a brand-new email with Send/Edit/Skip buttons."""
    import uuid
    import re

    email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
    if not email_match:
        return "I couldn't find an email address in your message. Try: 'Email john@example.com about ...'"

    to_email = email_match.group(0)

    client = get_supabase()
    contact_result = client.table("contacts").select("*").eq("email", to_email.lower()).maybe_single().execute()
    contact = contact_result.data if contact_result and contact_result.data else None
    to_name = contact["name"] if contact else to_email.split("@")[0].title()

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

    # Include conversation history so Claude knows prior context
    history = _load_conversation_history(limit=10)
    history_context = _format_history_for_context(history)

    prompt = (
        f"Garret wants to send a NEW email (not a reply).\n"
        f"Instruction: {text}\n"
        f"Recipient: {to_name} <{to_email}>\n"
        f"{contact_context}\n{tone_context}\n\n"
        f"{history_context}\n\n"
        "Generate TWO things, separated by the exact marker '---SUBJECT---':\n"
        "1. A short email subject line\n"
        "2. The email body\n\n"
        "Format:\n"
        "subject line here\n"
        "---SUBJECT---\n"
        "email body here\n\n"
        "Match Garret's tone. Keep it concise. No AI-sounding phrases.\n"
        "CRITICAL: Write in natural sentences and short paragraphs. NEVER use bullet points, "
        "dashes, numbered lists, or any structured formatting. Real people don't email in bullet points. "
        "Don't sign off with 'Best,' or 'Regards,' — just end naturally or with 'Thanks'."
    )

    raw = await ask_claude_complex(prompt, max_tokens=800)

    if "---SUBJECT---" in raw:
        parts = raw.split("---SUBJECT---", 1)
        subject = parts[0].strip()
        body = parts[1].strip()
    else:
        lines = raw.strip().split("\n", 1)
        subject = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else raw.strip()

    subject = subject.removeprefix("Subject:").strip()

    draft_id = str(uuid.uuid4())[:8]
    draft = {
        "body": body,
        "to": to_email,
        "to_name": to_name,
        "subject": subject,
        "thread_id": None,
        "in_reply_to": None,
        "is_new": True,
    }
    _store_draft(draft_id, draft)

    preview = (
        f"*New email to {to_name}:*\n\n"
        f"Subject: _{subject}_\n\n"
        f"`{body}`"
    )

    _save_message("assistant", f"[Drafted new email to {to_name}] Subject: {subject}\n{body[:200]}")

    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=send_confirmation_keyboard(draft_id),
    )
    return ""


async def _handle_send_last(text: str, update: Update) -> str:
    """Send the most recently drafted email when user says 'send' or 'send that'."""
    draft_id = _get_last_draft_id()
    if not draft_id:
        return "No pending draft to send. Draft an email first, then say 'send'."

    draft = _load_draft(draft_id)
    if not draft:
        return "The last draft has expired. Try drafting it again."

    try:
        from datetime import datetime, timezone

        if draft.get("is_new") or not draft.get("thread_id"):
            from app.gmail.client import send_new_email
            message_id = await send_new_email(
                to=draft["to"],
                subject=draft["subject"],
                body=draft["body"],
            )
        else:
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

        return f"✅ *Sent* to {draft.get('to_name', draft['to'])}."
    except Exception as e:
        logger.error("Email send failed", error=str(e))
        return f"❌ Failed to send: `{str(e)[:200]}`"


async def _handle_edit_last(text: str, update: Update) -> str:
    """Re-draft the last email based on user's feedback (e.g., 'make it shorter')."""
    draft_id = _get_last_draft_id()
    if not draft_id:
        return "No recent draft to edit. Draft an email first."

    # Load without deleting (peek)
    client = get_supabase()
    result = client.table("system_state").select("value").eq("key", f"draft:{draft_id}").maybe_single().execute()
    if not result.data:
        return "The last draft has expired. Try drafting it again."

    old_draft = result.data["value"]

    # Load conversation history for context
    history = _load_conversation_history(limit=10)
    history_context = _format_history_for_context(history)

    prompt = (
        f"Here is an email draft that Garret wants you to revise:\n\n"
        f"To: {old_draft.get('to_name', old_draft['to'])}\n"
        f"Subject: {old_draft['subject']}\n"
        f"Body:\n{old_draft['body']}\n\n"
        f"Garret's feedback: {text}\n\n"
        f"{history_context}\n\n"
        "Rewrite the email body incorporating the feedback. "
        "Return ONLY the revised email body — no subject line, no explanation.\n"
        "CRITICAL: Write in natural sentences and short paragraphs. NEVER use bullet points, "
        "dashes, numbered lists, or structured formatting. Sound warm and human."
    )

    new_body = await ask_claude_complex(prompt, max_tokens=800)

    # Update the draft
    import uuid
    new_draft_id = str(uuid.uuid4())[:8]
    new_draft = {**old_draft, "body": new_body.strip()}

    # Delete old draft, store new one
    _delete_draft(draft_id)
    _store_draft(new_draft_id, new_draft)

    preview = (
        f"*Revised draft to {new_draft.get('to_name', new_draft['to'])}:*\n\n"
        f"Subject: _{new_draft['subject']}_\n\n"
        f"`{new_draft['body']}`"
    )

    _save_message("assistant", f"[Revised draft] {new_draft['body'][:200]}")

    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=send_confirmation_keyboard(new_draft_id),
    )
    return ""


async def _handle_followup(text: str) -> str:
    """Parse a follow-up instruction and create a scheduled reminder."""
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
        return "I understood the follow-up request but couldn't parse the timing. Try: 'Follow up with Doug in 3 days about the memo.'"

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
            from app.gmail.client import send_new_email
            message_id = await send_new_email(
                to=draft["to"],
                subject=draft["subject"],
                body=draft["body"],
            )
        else:
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

        _save_message("assistant", f"✅ Sent email to {draft.get('to_name', draft['to'])}")

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
