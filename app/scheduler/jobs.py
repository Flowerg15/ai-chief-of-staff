"""
APScheduler setup.
Fires the daily brief at configured times.
"""
import asyncio
import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

_scheduler: BackgroundScheduler | None = None


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def _run_brief():
    """
    Sync wrapper to run the async brief from the scheduler thread.
    Submits to the running FastAPI event loop instead of creating a new one.
    Falls back to asyncio.run() if no loop is running.
    """
    from app.workflows.brief import generate_and_send_brief
    from app.telegram.bot import send_message

    async def _run():
        try:
            await generate_and_send_brief()
        except Exception as e:
            logger.error("Scheduled brief failed", error=str(e))
            try:
                await send_message(f"⚠️ *SYSTEM ALERT*: Daily brief failed.\n`{str(e)[:300]}`")
            except Exception:
                pass

    try:
        loop = asyncio.get_running_loop()
        # Submit to the existing event loop (FastAPI's)
        asyncio.run_coroutine_threadsafe(_run(), loop).result(timeout=120)
    except RuntimeError:
        # No running loop (shouldn't happen in production, but safe fallback)
        asyncio.run(_run())


def _run_followup_check():
    """
    Sync wrapper to check for due follow-ups from the scheduler thread.
    Runs daily, checks system_state for followup:* keys that are past due.
    """
    from app.telegram.bot import send_message
    from app.database.client import get_supabase
    from app.gmail.client import list_recent_emails

    async def _check():
        from datetime import datetime, timezone
        try:
            client = get_supabase()
            now = datetime.now(timezone.utc)

            # Fetch all pending follow-ups
            result = client.table("system_state").select("*").like("key", "followup:%").execute()
            followups = result.data or []

            for row in followups:
                value = row.get("value", {})
                if value.get("status") != "pending":
                    continue

                trigger_at = value.get("trigger_at")
                if not trigger_at:
                    continue

                trigger_dt = datetime.fromisoformat(trigger_at.replace("Z", "+00:00"))
                if now < trigger_dt:
                    continue  # Not due yet

                # Follow-up is due!
                contact = value.get("contact_name", "someone")
                subject = value.get("subject_hint", "")
                action = value.get("action", "check_reply")

                # Check if there's been a reply from this contact
                has_reply = False
                if contact and action == "check_reply":
                    try:
                        # Search recent emails for this contact's name
                        emails = await list_recent_emails(hours=int((now - trigger_dt).total_seconds() / 3600) + 72)
                        for email in emails:
                            sender = (email.sender or "").lower()
                            if contact.lower() in sender:
                                has_reply = True
                                break
                    except Exception:
                        pass

                if has_reply:
                    msg = f"✅ *Follow-up resolved:* {contact} replied re: {subject}. No action needed."
                else:
                    msg = (
                        f"⏰ *Follow-up due:* {contact} hasn't replied"
                        f"{' about ' + subject if subject else ''}.\n\n"
                        f"Want me to draft a nudge? Just say: *reply to {contact}*"
                    )

                await send_message(msg)

                # Mark as completed
                value["status"] = "completed"
                value["completed_at"] = now.isoformat()
                value["had_reply"] = has_reply
                client.table("system_state").upsert({
                    "key": row["key"],
                    "value": value,
                }).execute()

        except Exception as e:
            logger.error("Follow-up check failed", error=str(e))

    try:
        loop = asyncio.get_running_loop()
        asyncio.run_coroutine_threadsafe(_check(), loop).result(timeout=60)
    except RuntimeError:
        asyncio.run(_check())


def start_scheduler() -> None:
    global _scheduler
    _scheduler = BackgroundScheduler(timezone=settings.timezone)

    morning_hour, morning_min = _parse_time(settings.brief_time_morning)
    afternoon_hour, afternoon_min = _parse_time(settings.brief_time_afternoon)

    _scheduler.add_job(
        _run_brief,
        CronTrigger(hour=morning_hour, minute=morning_min),
        id="morning_brief",
        replace_existing=True,
    )
    _scheduler.add_job(
        _run_brief,
        CronTrigger(hour=afternoon_hour, minute=afternoon_min),
        id="afternoon_brief",
        replace_existing=True,
    )

    # Follow-up checker: runs 30 min after morning brief
    followup_hour = morning_hour
    followup_min = morning_min + 30
    if followup_min >= 60:
        followup_hour += 1
        followup_min -= 60

    _scheduler.add_job(
        _run_followup_check,
        CronTrigger(hour=followup_hour, minute=followup_min),
        id="followup_check",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started",
        morning=f"{morning_hour:02d}:{morning_min:02d}",
        afternoon=f"{afternoon_hour:02d}:{afternoon_min:02d}",
        followup_check=f"{followup_hour:02d}:{followup_min:02d}",
        timezone=settings.timezone,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
