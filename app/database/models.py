from pydantic import BaseModel, EmailStr
from datetime import datetime
from uuid import UUID


class Contact(BaseModel):
    id: UUID | None = None
    name: str
    email: str
    importance: int = 3
    company: str | None = None
    role: str | None = None
    deal_ids: list[UUID] = []
    notes: str | None = None
    last_interaction: datetime | None = None


class Deal(BaseModel):
    id: UUID | None = None
    name: str
    stage: str | None = None
    key_parties: list[str] = []
    thread_ids: list[UUID] = []
    key_dates: dict = {}
    decision_log: list[dict] = []
    notes: str | None = None


class Thread(BaseModel):
    id: UUID | None = None
    gmail_thread_id: str
    subject: str | None = None
    participants: list[str] = []
    summary: str | None = None
    deal_id: UUID | None = None
    contact_ids: list[UUID] = []
    last_updated: datetime | None = None
    waiting_on_garret: bool = False
    waiting_since: datetime | None = None


class EmailMessage(BaseModel):
    id: UUID | None = None
    gmail_message_id: str
    gmail_thread_id: str
    thread_id: UUID | None = None
    sender: str | None = None
    recipient: list[str] = []
    subject: str | None = None
    body_text: str | None = None
    attachments: list[dict] = []
    received_at: datetime | None = None
    processed: bool = False


class Decision(BaseModel):
    id: UUID | None = None
    date: datetime | None = None
    context: str | None = None
    decision: str
    rationale: str | None = None
    deal_id: UUID | None = None
    contact_ids: list[UUID] = []
    thread_id: UUID | None = None


class ToneSample(BaseModel):
    id: UUID | None = None
    category: str   # "formal_external" | "quick_internal" | "relationship"
    subject: str | None = None
    body: str
    to_name: str | None = None
    send_date: datetime | None = None
    is_active: bool = True
