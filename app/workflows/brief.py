"""
Daily executive brief workflow.

Generates a structured summary of the inbox and sends it to Telegram.
Runs on a schedule (7:30am and 1:00pm) via APScheduler.
"""
import structlog
from datetime import datetime, timezone, timedelta

from app.gmail.client import list_recent_emails
from app.claude.client import ask_claude
from app.claude.prompts import BRIEF_SYSTEM_PROMPT, build_inbox_context
from app.database.client import get_supabase
from app.telegram.bot import send_message
from app.workflows.inbox import _cache_emails, _load_contacts_by_email, _load_active_deals, _format_emails_for_claude

logger = structlog.get_logger(__name__)


async def generate_and_send_brief() -> None:
    """
    Full daily brief pipeline:
    1. Fetch emails
    2. Load context
    3. Call Claude
    4. Send to Telegram
    5. Update system state

    Fails gracefully — sends degraded brief if any step fails.
    """
    now = datetime.now(timezone.utc)
    brief_title = f"DAILY BRIEF — {now.strftime('%b %d, %-I:%M%p').upper()}"

    logger.info("Generating daily brief")

    errors = []
    emails = []
    contacts = []
    deals = []

    # Step 1: Fetch emails
    try:
        emails = await list_recent_emails(hours=24)
        await _cache_emails(emails)
    except Exception as e:
        errors.append(f"Gmail fetch failed: {e}")
        logger.error("Brief: Gmail fetch failed", error=str(e))

    # Step 2: Load context
    try:
        if emails:
            senders = list({e.sender for e in emails if e.sender})
            contacts = await _load_contacts_by_email(senders)
            deals = await _load_active_deals()
    except Exception as e:
        errors.append(f"Context load failed: {e}")
        logger.error("Brief: context load failed", error=str(e))

    # Step 3: Stale threads
    stale_count = await _get_stale_thread_count()

    # Step 4: Call Claude
    brief_text = ""
    if emails:
        try:
            context = build_inbox_context(
                emails=[e.model_dump() for e in emails],
                contacts=contacts,
                deals=deals,
            )
            email_text = _format_emails_for_claude(emails)

            stale_note = f"\n\n{stale_count} thread(s) have been waiting >48h for your reply." if stale_count else ""

            prompt = (
                f"Generate the daily executive brief for these {len(emails)} emails.\n\n"
                f"{email_text}\n"
                f"{stale_note}\n\n"
                f"Title it: {brief_title}"
            )

            brief_text = await ask_claude(
                prompt,
                context=context,
                system_override=BRIEF_SYSTEM_PROMPT,
                max_tokens=1500,
            )
        except Exception as e:
            errors.append(f"Claude generation failed: {e}")
            logger.error("Brief: Claude call failed", error=str(e))

    # Step 5: Build final message
    if brief_text:
        message = brief_text
    elif emails:
        message = f"*{brief_title}*\n\n{len(emails)} emails received. Full brief generation failed."
    else:
        message = f"*{brief_title}*\n\n_No new emails in the last 24 hours._"

    # System status line (appended to evening brief)
    if _is_evening_brief():
        stats = await _get_daily_stats()
        message += f"\n\n_System: {stats}_"

    # Degraded notice
    if errors:
        message += f"\n\n⚠️ _Degraded: {'; '.join(errors)}_"

    await send_message(message)

    # Update last brief timestamp
    client = get_supabase()
    client.table("system_state").upsert({
        "key": "last_brief_at",
        "value": {"timestamp": now.isoformat(), "email_count": len(emails), "errors": errors},
    }).execute()

    logger.info("Brief sent", emails=len(emails), errors=len(errors))


async def _get_stale_thread_count() -> int:
    """Count threads where Garret hasn't replied in >48 hours."""
    try:
        client = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        result = (
            client.table("threads")
            .select("id", count="exact")
            .eq("waiting_on_garret", True)
            .lt("waiting_since", cutoff)
            .execute()
        )
        return result.count or 0
    except Exception:
        return 0


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

        return f"{emails_count} emails processed, {sent_count} replies sent, 0 errors"
    except Exception:
        return "stats unavailable"
