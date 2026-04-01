"""
APScheduler setup.
Fires the daily brief at configured times.
"""
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

_scheduler: AsyncIOScheduler | None = None


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


async def _run_brief():
    """Run the async brief directly on the event loop."""
    from app.workflows.brief import generate_and_send_brief
    from app.telegram.bot import send_message

    try:
        await generate_and_send_brief()
    except Exception as e:
        logger.error("Scheduled brief failed", error=str(e))
        try:
            await send_message(f"⚠️ *SYSTEM ALERT*: Daily brief failed.\n`{str(e)[:300]}`")
        except Exception:
            pass  # Don't let the alert itself crash the scheduler


def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=settings.timezone)

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

    _scheduler.start()
    logger.info(
        "Scheduler started",
        morning=f"{morning_hour:02d}:{morning_min:02d}",
        afternoon=f"{afternoon_hour:02d}:{afternoon_min:02d}",
        timezone=settings.timezone,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
