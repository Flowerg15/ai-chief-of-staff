"""
Inbox intelligence workflow.

Responsibilities:
- Pull recent emails from Gmail
- Cache them in Supabase
- Look up known contacts and deals for context
- Return a structured summary via Claude
"""
import structlog
from datetime import datetime, timezone

from app.gmail.client import list_recent_emails
from app.database.client import get_supabase
from app.database.models import EmailMessage
from app.claude.client import ask_claude
from app.claude.prompts import build_inbox_context, build_query_context

logger = structlog.get_logger(__name__)


async def summarise_inbox(hours: int = 48, **kwargs) -> str:
    """
    Fetch recent emails, build context, and return a Claude summary.
    Called both on-demand ('Summarise my inbox') and by the daily brief.
    """
    # 1. Fetch emails from Gmail
    emails = await list_recent_emails(hours=hours)
    if not emails:
        return "_No new emails in the last 48 hours._"

    # 2. Cache emails in Supabase (skip ones already stored)
    await _cache_emails(emails)

    # 3. Load known contacts that appear in these emails
    senders = list({e.sender for e in emails if e.sender})
    contacts = await _load_contacts_by_email(senders)

    # 4. Load active deals for context
    deals = await _load_active_deals()

    # 5. Build context and call Claude
    context = build_inbox_context(
        emails=[e.model_dump() for e in emails],
        contacts=contacts,
        deals=deals,
    )

    email_text = _format_emails_for_claude(emails)
    prompt = (
        f"Here are the {len(emails)} emails from the last {hours} hours:\n\n"
        f"{email_text}\n\n"
        "Summarise what matters. Be opinionated. Tell me what to act on and what to ignore."
    )

    return await ask_claude(prompt, context=context, max_tokens=1500)


async def retrieve_context_for_query(query: str) -> str:
    """
    Given a free-text query, retrieve relevant contacts, deals, and threads.
    Returns a formatted context string for injection into Claude's prompt.
    """
    client = get_supabase()
    query_lower = query.lower()

    # Named entity lookup: check if query mentions a known contact
    contacts_result = client.table("contacts").select("*").execute()
    all_contacts = contacts_result.data or []
    matched_contacts = [
        c for c in all_contacts
        if c["name"].lower() in query_lower or (c.get("email") or "").split("@")[0].lower() in query_lower
    ]

    # Deal lookup
    deals_result = client.table("deals").select("*").execute()
    all_deals = deals_result.data or []
    matched_deals = [
        d for d in all_deals
        if d["name"].lower() in query_lower
    ]

    # Thread lookup for matched contacts/deals
    threads = []
    if matched_contacts:
        contact_ids = [c["id"] for c in matched_contacts]
        for cid in contact_ids[:2]:
            t_result = client.table("threads").select("*").contains("contact_ids", [cid]).limit(3).execute()
            threads.extend(t_result.data or [])

    if matched_deals:
        deal_ids = [d["id"] for d in matched_deals]
        for did in deal_ids[:2]:
            t_result = client.table("threads").select("*").eq("deal_id", did).limit(3).execute()
            threads.extend(t_result.data or [])

    return build_query_context(
        threads=threads[:5],
        contacts=matched_contacts[:3],
        deals=matched_deals[:3],
    )


async def _cache_emails(emails: list[EmailMessage]) -> None:
    """Store emails in Supabase, skipping duplicates. Also tracks waiting_on_garret."""
    from app.config import get_settings
    settings = get_settings()
    my_email = settings.gmail_user_email.lower()
    client = get_supabase()

    for email in emails:
        try:
            client.table("email_cache").upsert(
                {
                    "gmail_message_id": email.gmail_message_id,
                    "gmail_thread_id": email.gmail_thread_id,
                    "sender": email.sender,
                    "recipient": email.recipient,
                    "subject": email.subject,
                    "body_text": email.body_text,
                    "attachments": email.attachments,
                    "received_at": email.received_at.isoformat() if email.received_at else None,
                },
                on_conflict="gmail_message_id",
            ).execute()
        except Exception as e:
            logger.warning("Failed to cache email", message_id=email.gmail_message_id, error=str(e))

    # Update waiting_on_garret for threads with inbound emails
    _update_waiting_status(emails, my_email, client)


def _update_waiting_status(emails: list[EmailMessage], my_email: str, client) -> None:
    """
    Mark threads as waiting_on_garret if the latest message is FROM someone else.
    Clear the flag if the latest message is FROM Garret.
    """
    from datetime import datetime, timezone

    # Group by thread, keep only the latest email per thread
    thread_latest: dict[str, EmailMessage] = {}
    for email in emails:
        tid = email.gmail_thread_id
        if tid not in thread_latest or (email.received_at and thread_latest[tid].received_at and email.received_at > thread_latest[tid].received_at):
            thread_latest[tid] = email

    for tid, latest in thread_latest.items():
        sender = (latest.sender or "").lower()
        # Extract email from "Name <email>" format
        if "<" in sender:
            sender = sender.split("<")[1].rstrip(">").strip()

        is_from_me = my_email in sender
        try:
            # Check if thread exists in DB
            existing = client.table("threads").select("id").eq("gmail_thread_id", tid).maybe_single().execute()
            if existing.data:
                client.table("threads").update({
                    "waiting_on_garret": not is_from_me,
                    "waiting_since": datetime.now(timezone.utc).isoformat() if not is_from_me else None,
                }).eq("id", existing.data["id"]).execute()
        except Exception as e:
            logger.debug("Could not update thread waiting status", thread_id=tid, error=str(e))


async def _load_contacts_by_email(email_addresses: list[str]) -> list[dict]:
    """Look up known contacts by their email address."""
    if not email_addresses:
        return []
    client = get_supabase()
    # Extract just the email part (some senders come as "Name <email@domain.com>")
    clean_emails = []
    for addr in email_addresses:
        if "<" in addr:
            addr = addr.split("<")[1].rstrip(">").strip()
        clean_emails.append(addr.lower())

    result = client.table("contacts").select("*").in_("email", clean_emails).execute()
    return result.data or []


async def _load_active_deals() -> list[dict]:
    """Load deals that are not closed."""
    client = get_supabase()
    result = (
        client.table("deals")
        .select("*")
        .not_.in_("stage", ["Closed", "Dead", "Passed"])
        .order("updated_at", desc=True)
        .limit(10)
        .execute()
    )
    return result.data or []


def _format_emails_for_claude(emails: list[EmailMessage]) -> str:
    """Format emails into a readable block for Claude's context window."""
    lines = []
    for i, email in enumerate(emails, 1):
        received = email.received_at.strftime("%b %d, %H:%M") if email.received_at else "unknown time"
        attachments_note = f" [{len(email.attachments)} attachment(s)]" if email.attachments else ""
        lines.append(f"[{i}] From: {email.sender or 'unknown'}")
        lines.append(f"    Subject: {email.subject or '(no subject)'}{attachments_note}")
        lines.append(f"    Received: {received}")
        lines.append(f"    Body: {(email.body_text or '')[:400].strip()}")
        lines.append("")
    return "\n".join(lines)
