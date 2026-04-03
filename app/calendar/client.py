"""
Google Calendar API client.
Uses the same OAuth credentials as Gmail (shared token).
"""
import structlog
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.gmail.auth import get_credentials
from app.config import get_settings

logger = structlog.get_logger(__name__)


async def _get_service():
    creds = await get_credentials()
    return build("calendar", "v3", credentials=creds)


async def get_todays_events() -> list[dict]:
    """Get today's calendar events in the user's timezone."""
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

    return await list_events(
        time_min=start_of_day,
        time_max=end_of_day,
    )


async def get_upcoming_events(hours: int = 4) -> list[dict]:
    """Get events happening in the next N hours."""
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    end = now + timedelta(hours=hours)

    return await list_events(time_min=now, time_max=end)


async def list_events(
    time_min: datetime,
    time_max: datetime,
    max_results: int = 20,
) -> list[dict]:
    """
    List calendar events in a time range.
    Returns simplified event dicts.
    """
    try:
        service = await _get_service()
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        ).execute()

        events = []
        for item in result.get("items", []):
            start = item.get("start", {})
            end = item.get("end", {})

            # Parse start time
            if start.get("dateTime"):
                start_dt = datetime.fromisoformat(start["dateTime"])
                start_display = start_dt.strftime("%-I:%M%p").lower()
                all_day = False
            elif start.get("date"):
                start_display = "all day"
                all_day = True
            else:
                start_display = "unknown"
                all_day = False

            # Parse end time
            if end.get("dateTime"):
                end_dt = datetime.fromisoformat(end["dateTime"])
                end_display = end_dt.strftime("%-I:%M%p").lower()
            else:
                end_display = ""

            # Get attendees
            attendees = []
            for a in item.get("attendees", []):
                if not a.get("self"):
                    attendees.append({
                        "name": a.get("displayName", a.get("email", "")),
                        "email": a.get("email", ""),
                        "status": a.get("responseStatus", "needsAction"),
                    })

            events.append({
                "id": item.get("id"),
                "summary": item.get("summary", "(no title)"),
                "description": (item.get("description") or "")[:500],
                "location": item.get("location", ""),
                "start": start_display,
                "end": end_display,
                "all_day": all_day,
                "attendees": attendees,
                "hangout_link": item.get("hangoutLink", ""),
                "html_link": item.get("htmlLink", ""),
            })

        logger.info("Fetched calendar events", count=len(events))
        return events

    except HttpError as e:
        if e.resp.status == 401:
            logger.error("Calendar auth failed — may need re-authorization with calendar scope")
            raise RuntimeError("Calendar not authorized. Please re-authorize at /gmail/oauth/start")
        logger.error("Calendar API error", status=e.resp.status, error=str(e))
        raise RuntimeError(f"Calendar API error: {str(e)[:200]}")
    except Exception as e:
        logger.error("Calendar fetch failed", error=str(e))
        raise RuntimeError(f"Calendar error: {str(e)[:200]}")


async def create_event(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
    attendees: list[str] | None = None,
    location: str = "",
    add_meet: bool = True,
) -> dict:
    """
    Create a new calendar event with an auto-attached Google Meet link.
    Returns the created event dict.
    """
    service = await _get_service()

    event_body = {
        "summary": summary,
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": get_settings().timezone,
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": get_settings().timezone,
        },
    }

    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location
    if attendees:
        event_body["attendees"] = [{"email": a} for a in attendees]

    # Auto-attach Google Meet link
    if add_meet:
        import uuid
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    result = service.events().insert(
        calendarId="primary",
        body=event_body,
        sendUpdates="all" if attendees else "none",
        conferenceDataVersion=1 if add_meet else 0,
    ).execute()

    meet_link = result.get("hangoutLink", "")
    logger.info(
        "Calendar event created",
        event_id=result["id"],
        summary=summary,
        meet_link=meet_link,
    )
    return result


def format_events_for_context(events: list[dict]) -> str:
    """Format calendar events into a readable context block for Claude."""
    if not events:
        return "No calendar events."

    lines = []
    for e in events:
        time_str = f"{e['start']}" if e["all_day"] else f"{e['start']}–{e['end']}"
        attendee_str = ""
        if e["attendees"]:
            names = [a["name"] for a in e["attendees"][:5]]
            attendee_str = f" with {', '.join(names)}"
        lines.append(f"• {time_str}: {e['summary']}{attendee_str}")
        if e.get("location"):
            lines.append(f"  Location: {e['location']}")

    return "\n".join(lines)
