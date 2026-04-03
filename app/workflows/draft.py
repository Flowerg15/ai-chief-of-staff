"""
Email drafting workflow.

Given a user instruction like "Reply to Doug about the QSBS memo",
this workflow:
1. Finds the relevant thread
2. Loads contact + deal context
3. Selects matching tone samples
4. Calls Claude to draft the reply
5. Returns the draft dict for Telegram to display
"""
import structlog
from app.database.client import get_supabase
from app.claude.client import ask_claude_complex
from app.claude.prompts import build_draft_context
from app.gmail.client import get_thread

logger = structlog.get_logger(__name__)


async def draft_reply(instruction: str) -> dict | None:
    """
    Parse the instruction, find the thread, draft a reply.
    Returns a dict with keys: body, to, to_name, subject, thread_id, in_reply_to
    Or None if the thread can't be identified.
    """
    client = get_supabase()

    # Step 1: Find the relevant thread from the instruction
    thread_info = await _find_thread(instruction)
    if not thread_info:
        logger.warning("Could not identify thread for draft", instruction=instruction)
        return None

    gmail_thread_id = thread_info.get("gmail_thread_id")
    thread_db = thread_info.get("db_record", {})

    # Step 2: Fetch thread messages from Gmail
    messages = await get_thread(gmail_thread_id)
    if not messages:
        return None

    latest_message = messages[-1]
    reply_to_sender = latest_message.sender or ""

    # Step 3: Load contact
    contact = await _load_contact(reply_to_sender)

    # Step 4: Load deal
    deal = None
    if thread_db.get("deal_id"):
        deal_result = client.table("deals").select("*").eq("id", thread_db["deal_id"]).maybe_single().execute()
        deal = deal_result.data

    # Step 5: Load tone samples matching context
    category = _classify_tone_category(contact, deal)
    tone_result = (
        client.table("tone_samples")
        .select("*")
        .eq("category", category)
        .eq("is_active", True)
        .limit(3)
        .execute()
    )
    tone_samples = tone_result.data or []

    # Step 6: Build context and draft
    context = build_draft_context(
        thread_messages=[m.model_dump() for m in messages],
        contact=contact,
        deal=deal,
        tone_samples=tone_samples,
    )

    prompt = (
        f"The user wants to: {instruction}\n\n"
        "Draft a reply to the latest email in this thread. "
        "Return ONLY the email body — no subject line, no greeting prefix, no explanation. "
        "Match the tone examples exactly.\n\n"
        "CRITICAL STYLE RULES:\n"
        "- Write in natural flowing sentences and short paragraphs. NEVER use bullet points, "
        "dashes, numbered lists, or any structured formatting. Real people don't email in bullet points.\n"
        "- Don't sign off with 'Best,' or 'Regards,' or 'Warm regards,' — just end naturally or with 'Thanks'.\n"
        "- Sound warm, human, and efficient — like a busy founder who's genuinely friendly."
    )

    body = await ask_claude_complex(prompt, context=context, max_tokens=800)

    # Extract reply-to email address
    to_email = reply_to_sender
    to_name = contact["name"] if contact else reply_to_sender
    if "<" in reply_to_sender:
        to_name = reply_to_sender.split("<")[0].strip()
        to_email = reply_to_sender.split("<")[1].rstrip(">").strip()

    return {
        "body": body.strip(),
        "to": to_email,
        "to_name": to_name,
        "subject": f"Re: {latest_message.subject or ''}",
        "thread_id": gmail_thread_id,
        "in_reply_to": latest_message.gmail_message_id,
    }


async def _find_thread(instruction: str) -> dict | None:
    """
    Find the most relevant thread for the draft instruction.
    Uses name/keyword matching against cached threads.
    """
    client = get_supabase()
    instruction_lower = instruction.lower()

    # Check contact names mentioned in the instruction
    contacts_result = client.table("contacts").select("id, name, email").execute()
    for contact in (contacts_result.data or []):
        if contact["name"].lower() in instruction_lower:
            # Find most recent thread involving this contact
            thread_result = (
                client.table("threads")
                .select("*")
                .contains("participants", [contact["email"]])
                .order("last_updated", desc=True)
                .limit(1)
                .execute()
            )
            if thread_result.data:
                return {
                    "gmail_thread_id": thread_result.data[0]["gmail_thread_id"],
                    "db_record": thread_result.data[0],
                }

    # Fall back: check recent email cache for sender name mentions
    cache_result = (
        client.table("email_cache")
        .select("gmail_thread_id, sender, subject")
        .order("received_at", desc=True)
        .limit(20)
        .execute()
    )
    for email in (cache_result.data or []):
        sender = email.get("sender", "") or ""
        subject = email.get("subject", "") or ""
        if any(word in sender.lower() or word in subject.lower()
               for word in instruction_lower.split()
               if len(word) > 3):
            return {
                "gmail_thread_id": email["gmail_thread_id"],
                "db_record": {},
            }

    return None


async def _load_contact(sender_raw: str) -> dict | None:
    """Look up a contact from their raw sender string."""
    email = sender_raw
    if "<" in sender_raw:
        email = sender_raw.split("<")[1].rstrip(">").strip()
    email = email.lower()

    client = get_supabase()
    result = client.table("contacts").select("*").eq("email", email).maybe_single().execute()
    return result.data


def _classify_tone_category(contact: dict | None, deal: dict | None) -> str:
    """Choose the right tone category based on context."""
    if deal:
        return "formal_external"
    if not contact:
        return "formal_external"
    importance = contact.get("importance", 3)
    if importance >= 4:
        return "formal_external"
    return "quick_internal"
