"""
Daily executive brief workflow — PROACTIVE edition.

Generates a structured summary of the inbox and sends it to Telegram.
Key difference from v1: pre-drafts replies for stale threads and
includes urgency-scored context so the brief DRIVES action.

Runs on a schedule (7:30am and 1:00pm) via APScheduler.
"""
import uuid
import structlog
from datetime import datetime, timezone, timedelta

from app.gmail.client import list_recent_emails, get_thread
from app.claude.client import ask_claude, ask_claude_complex
from app.claude.prompts import BRIEF_SYSTEM_PROMPT, build_inbox_context, build_draft_context
from app.database.client import get_supabase
from app.telegram.bot import send_message
from app.telegram.keyboards import send_confirmation_keyboard
from app.workflows.inbox import (
    _cache_emails,
    _load_contacts_by_email,
    _load_active_deals,
    _format_emails_for_claude,
)
from app.calendar.client import get_todays_events, format_events_for_context

logger = structlog.get_logger(__name__)


async def generate_and_send_brief() -> None:
    """
    Full daily brief pipeline:
    1. Fetch stale threads (waiting on Garret)
    2. Pre-draft replies for top stale threads
    3. Fetch new emails
    4. Load context (contacts, deals)
    5. Call Claude with urgency-enriched prompt
    6. Send brief + inline draft buttons to Telegram
    7. Update system state
    """
    from zoneinfo import ZoneInfo
    from app.config import get_settings

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    brief_title = f"DAILY BRIEF — {now.strftime('%b %d, %-I:%M%p').upper()}"

    logger.info("Generating proactive daily brief")

    errors = []
    emails = []
    contacts = []
    deals = []
    stale_threads = []
    pre_drafts = []
    calendar_events = []

    # Step 0: Fetch today's calendar
    try:
        calendar_events = await get_todays_events()
        logger.info("Calendar events loaded", count=len(calendar_events))
    except Exception as e:
        errors.append(f"Calendar fetch failed: {e}")
        logger.error("Brief: calendar fetch failed", error=str(e))

    # Step 1: Fetch stale threads (waiting on Garret)
    try:
        stale_threads = await _get_stale_threads()
        logger.info("Stale threads found", count=len(stale_threads))
    except Exception as e:
        errors.append(f"Stale thread lookup failed: {e}")
        logger.error("Brief: stale thread lookup failed", error=str(e))

    # Step 2: Pre-draft replies for top 3 stale threads
    if stale_threads:
        try:
            pre_drafts = await _pre_draft_stale_replies(stale_threads[:3])
            logger.info("Pre-drafted replies", count=len(pre_drafts))
        except Exception as e:
            errors.append(f"Pre-draft generation failed: {e}")
            logger.error("Brief: pre-draft failed", error=str(e))

    # Step 3: Fetch new emails
    try:
        emails = await list_recent_emails(hours=24)
        await _cache_emails(emails)
    except Exception as e:
        errors.append(f"Gmail fetch failed: {e}")
        logger.error("Brief: Gmail fetch failed", error=str(e))

    # Step 4: Load context
    try:
        if emails:
            senders = list({e.sender for e in emails if e.sender})
            contacts = await _load_contacts_by_email(senders)
            deals = await _load_active_deals()
    except Exception as e:
        errors.append(f"Context load failed: {e}")
        logger.error("Brief: context load failed", error=str(e))

    # Step 5: Build urgency-enriched prompt and call Claude
    brief_text = ""
    try:
        context = build_inbox_context(
            emails=[e.model_dump() for e in emails] if emails else [],
            contacts=contacts,
            deals=deals,
        )

        # Format stale threads for the prompt
        stale_section = _format_stale_for_prompt(stale_threads)

        # Format new emails
        email_text = _format_emails_for_claude(emails) if emails else "No new emails in the last 24 hours."

        # Format calendar
        calendar_section = format_events_for_context(calendar_events) if calendar_events else "No events today."

        prompt = (
            f"Generate the daily executive brief.\n\n"
            f"Title: {brief_title}\n\n"
            f"=== TODAY'S CALENDAR ===\n{calendar_section}\n\n"
            f"=== THREADS WAITING ON GARRET ===\n{stale_section}\n\n"
            f"=== NEW EMAILS (last 24h): {len(emails)} ===\n{email_text}\n\n"
        )

        if pre_drafts:
            draft_section = "\n".join(
                f"DRAFT for {d['to_name']} re: {d['subject']}\n"
                f"Body: {d['body'][:300]}"
                for d in pre_drafts
            )
            prompt += f"=== PRE-DRAFTED REPLIES ===\n{draft_section}\n\n"

        prompt += "Generate the brief now. Remember: Waiting On You section goes FIRST."

        brief_text = await ask_claude(
            prompt,
            context=context,
            system_override=BRIEF_SYSTEM_PROMPT,
            max_tokens=2000,
        )
    except Exception as e:
        errors.append(f"Claude generation failed: {e}")
        logger.error("Brief: Claude call failed", error=str(e))

    # Step 6: Build final message
    if brief_text:
        message = brief_text
    elif emails or stale_threads:
        message = (
            f"*{brief_title}*\n\n"
            f"{'🔴 ' + str(len(stale_threads)) + ' thread(s) waiting on you. ' if stale_threads else ''}"
            f"{str(len(emails)) + ' new emails.' if emails else 'No new emails.'}\n"
            f"Full brief generation failed."
        )
    else:
        message = f"*{brief_title}*\n\n_No new emails and no pending threads. You're clear._"

    # Append system stats for evening brief
    if _is_evening_brief():
        stats = await _get_daily_stats()
        message += f"\n\n_System: {stats}_"

    # Degraded notice
    if errors:
        message += f"\n\n⚠️ _Degraded: {'; '.join(errors)}_"

    # Send the brief text
    await send_message(message)

    # Step 7: Send pre-drafted replies as separate messages with Send buttons
    for draft in pre_drafts:
        try:
            from app.telegram.handlers import _store_draft
            draft_id = str(uuid.uuid4())[:8]
            _store_draft(draft_id, draft)

            draft_msg = (
                f"📝 *Draft reply to {draft.get('to_name', draft['to'])}:*\n\n"
                f"`{draft['body'][:1500]}`\n\n"
                f"Re: _{draft['subject']}_"
            )
            await send_message(draft_msg, reply_markup=send_confirmation_keyboard(draft_id))
        except Exception as e:
            logger.error("Failed to send pre-draft", to=draft.get("to"), error=str(e))

    # Update last brief timestamp
    client = get_supabase()
    client.table("system_state").upsert({
        "key": "last_brief_at",
        "value": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "email_count": len(emails),
            "stale_count": len(stale_threads),
            "drafts_generated": len(pre_drafts),
            "errors": errors,
        },
    }).execute()

    logger.info(
        "Brief sent",
        emails=len(emails),
        stale=len(stale_threads),
        drafts=len(pre_drafts),
        errors=len(errors),
    )


async def _get_stale_threads() -> list[dict]:
    """
    Fetch threads where Garret hasn't replied, with full context.
    Returns threads ordered by wait time (longest first).
    """
    client = get_supabase()
    result = (
        client.table("threads")
        .select("*")
        .eq("waiting_on_garret", True)
        .not_.is_("waiting_since", "null")
        .order("waiting_since", desc=False)  # Oldest first = most overdue
        .limit(10)
        .execute()
    )

    threads = result.data or []
    now = datetime.now(timezone.utc)

    # Enrich with wait duration
    for t in threads:
        if t.get("waiting_since"):
            waiting_since = datetime.fromisoformat(t["waiting_since"].replace("Z", "+00:00"))
            delta = now - waiting_since
            days = delta.days
            hours = delta.seconds // 3600
            t["wait_display"] = f"{days}d {hours}h" if days else f"{hours}h"
            t["wait_hours"] = delta.total_seconds() / 3600
        else:
            t["wait_display"] = "unknown"
            t["wait_hours"] = 0

    return threads


async def _pre_draft_stale_replies(stale_threads: list[dict]) -> list[dict]:
    """
    For the top stale threads, fetch the thread history and generate
    a draft reply. Returns a list of draft dicts ready for Send buttons.
    """
    from app.workflows.draft import _load_contact, _classify_tone_category

    client = get_supabase()
    drafts = []

    for thread in stale_threads:
        try:
            gmail_thread_id = thread.get("gmail_thread_id")
            if not gmail_thread_id:
                continue

            # Fetch thread messages
            messages = await get_thread(gmail_thread_id)
            if not messages:
                continue

            latest = messages[-1]
            sender = latest.sender or ""

            # Load context
            contact = await _load_contact(sender)
            deal = None
            if thread.get("deal_id"):
                deal_result = client.table("deals").select("*").eq("id", thread["deal_id"]).maybe_single().execute()
                deal = deal_result.data

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

            context = build_draft_context(
                thread_messages=[m.model_dump() for m in messages],
                contact=contact,
                deal=deal,
                tone_samples=tone_samples,
            )

            wait_info = thread.get("wait_display", "")
            prompt = (
                f"This thread has been waiting {wait_info} for Garret's reply.\n"
                f"The latest message is from {sender}.\n\n"
                "Draft a reply. Be concise — this is a catch-up response, not an essay. "
                "If the thread is just informational, a quick acknowledgment is fine. "
                "Return ONLY the email body — no subject line, no greeting prefix, no explanation. "
                "Match the tone examples exactly.\n\n"
                "CRITICAL: Write in natural sentences and short paragraphs. NEVER use bullet points, "
                "dashes, numbered lists, or structured formatting. Don't sign off with 'Best,' or "
                "'Regards,' — just end naturally. Sound warm, human, and efficient."
            )

            body = await ask_claude_complex(prompt, context=context, max_tokens=500)

            # Build draft dict
            to_email = sender
            to_name = contact["name"] if contact else sender
            if "<" in sender:
                to_name = sender.split("<")[0].strip()
                to_email = sender.split("<")[1].rstrip(">").strip()

            drafts.append({
                "body": body.strip(),
                "to": to_email,
                "to_name": to_name,
                "subject": f"Re: {latest.subject or ''}",
                "thread_id": gmail_thread_id,
                "in_reply_to": latest.gmail_message_id,
            })

        except Exception as e:
            logger.warning(
                "Failed to pre-draft for stale thread",
                thread_id=thread.get("gmail_thread_id"),
                error=str(e),
            )

    return drafts


def _format_stale_for_prompt(stale_threads: list[dict]) -> str:
    """Format stale threads into a readable block for Claude."""
    if not stale_threads:
        return "None — you're all caught up."

    lines = []
    for t in stale_threads:
        participants = ", ".join(t.get("participants", [])[:3])
        lines.append(
            f"• {t.get('subject', 'No subject')} — {participants}\n"
            f"  Waiting: {t.get('wait_display', 'unknown')} | "
            f"Deal: {t.get('deal_id', 'none')}"
        )
    return "\n".join(lines)


def _is_evening_brief() -> bool:
    """True if this is the afternoon/evening brief."""
    from zoneinfo import ZoneInfo
    from app.config import get_settings
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now_hour = datetime.now(tz).hour
    brief_hour = int(settings.brief_time_afternoon.split(":")[0])
    return abs(now_hour - brief_hour) <= 1


async def _get_daily_stats() -> str:
    """Return a one-line system status for the evening brief."""
    try:
        client = get_supabase()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()

        emails_result = client.table("email_cache").select("id", count="exact").gte("created_at", today_start).execute()
        emails_count = emails_result.count or 0

        sent_result = client.table("audit_log").select("id", count="exact").eq("action", "email_sent").gte("created_at", today_start).execute()
        sent_count = sent_result.count or 0

        return f"{emails_count} emails processed, {sent_count} replies sent"
    except Exception:
        return "stats unavailable"
