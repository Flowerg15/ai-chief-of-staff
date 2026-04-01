"""
Gmail API client.
Wraps google-api-python-client with clean async-friendly methods.
"""
import base64
import email as email_lib
import structlog
from datetime import datetime, timezone, timedelta
from typing import Any
from googleapiclient.discovery import build

from app.gmail.auth import get_credentials
from app.database.models import EmailMessage

logger = structlog.get_logger(__name__)


async def _get_service():
    creds = await get_credentials()
    return build("gmail", "v1", credentials=creds)


def _parse_headers(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers}


def _decode_body(payload: dict) -> str:
    """Extract plain text body from a Gmail message payload."""
    def _find_text(part: dict, mime: str) -> str | None:
        if part.get("mimeType") == mime:
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for subpart in part.get("parts", []):
            result = _find_text(subpart, mime)
            if result:
                return result
        return None

    # Prefer plain text; fall back to HTML
    return _find_text(payload, "text/plain") or _find_text(payload, "text/html") or ""


def _extract_attachments(payload: dict) -> list[dict]:
    """Extract attachment metadata from a Gmail message payload."""
    attachments = []

    def _walk(part: dict):
        filename = part.get("filename")
        if filename and part.get("body", {}).get("attachmentId"):
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType"),
                "size": part.get("body", {}).get("size", 0),
                "attachment_id": part["body"]["attachmentId"],
            })
        for subpart in part.get("parts", []):
            _walk(subpart)

    _walk(payload)
    return attachments


async def list_recent_emails(hours: int = 48) -> list[EmailMessage]:
    """
    Pull emails from the last N hours via Gmail API.
    Returns a list of EmailMessage objects (body included).
    """
    service = await _get_service()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    query = f"after:{int(cutoff.timestamp())}"

    results = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=50,
    ).execute()

    messages_meta = results.get("messages", [])
    emails: list[EmailMessage] = []

    for meta in messages_meta:
        try:
            msg = service.users().messages().get(
                userId="me",
                id=meta["id"],
                format="full",
            ).execute()

            headers = _parse_headers(msg.get("payload", {}).get("headers", []))
            body = _decode_body(msg.get("payload", {}))
            attachments = _extract_attachments(msg.get("payload", {}))

            received_ts = int(msg.get("internalDate", 0)) / 1000
            received_at = datetime.fromtimestamp(received_ts, tz=timezone.utc) if received_ts else None

            # Parse recipients
            to_raw = headers.get("to", "")
            recipients = [r.strip() for r in to_raw.split(",") if r.strip()]

            emails.append(EmailMessage(
                gmail_message_id=msg["id"],
                gmail_thread_id=msg["threadId"],
                sender=headers.get("from"),
                recipient=recipients,
                subject=headers.get("subject"),
                body_text=body[:8000],  # Cap at 8k chars per email
                attachments=attachments,
                received_at=received_at,
            ))
        except Exception as e:
            logger.error("Failed to fetch email", message_id=meta["id"], error=str(e))

    logger.info("Fetched emails from Gmail", count=len(emails), hours=hours)
    return emails


async def get_thread(gmail_thread_id: str) -> list[EmailMessage]:
    """Fetch all messages in a Gmail thread."""
    service = await _get_service()
    thread = service.users().threads().get(userId="me", id=gmail_thread_id, format="full").execute()

    emails = []
    for msg in thread.get("messages", []):
        headers = _parse_headers(msg.get("payload", {}).get("headers", []))
        body = _decode_body(msg.get("payload", {}))
        received_ts = int(msg.get("internalDate", 0)) / 1000
        received_at = datetime.fromtimestamp(received_ts, tz=timezone.utc) if received_ts else None

        emails.append(EmailMessage(
            gmail_message_id=msg["id"],
            gmail_thread_id=gmail_thread_id,
            sender=headers.get("from"),
            subject=headers.get("subject"),
            body_text=body[:8000],
            received_at=received_at,
        ))

    return emails


async def send_reply(
    thread_id: str,
    message_id: str,
    to: str,
    subject: str,
    body: str,
) -> str:
    """
    Send a reply in an existing thread.
    Returns the new message ID.
    """
    service = await _get_service()
    settings_email = (await get_credentials()).token  # Just to confirm auth

    # Build RFC 2822 message
    raw_message = f"""To: {to}
Subject: {subject}
In-Reply-To: {message_id}
References: {message_id}
Content-Type: text/plain; charset=utf-8

{body}"""

    encoded = base64.urlsafe_b64encode(raw_message.encode("utf-8")).decode("utf-8")
    result = service.users().messages().send(
        userId="me",
        body={"raw": encoded, "threadId": thread_id},
    ).execute()

    logger.info("Email sent", message_id=result["id"], thread_id=thread_id, to=to)
    return result["id"]


async def download_attachment(message_id: str, attachment_id: str) -> bytes:
    """Download an email attachment and return its raw bytes."""
    service = await _get_service()
    attachment = service.users().messages().attachments().get(
        userId="me",
        messageId=message_id,
        id=attachment_id,
    ).execute()
    return base64.urlsafe_b64decode(attachment["data"] + "==")
