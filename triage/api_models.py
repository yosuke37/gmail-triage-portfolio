from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from triage.core import Label


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReviewRequest(RequestModel):
    label: Label
    approve: bool


class ReadRequest(RequestModel):
    unread: bool


class DraftRequest(RequestModel):
    instruction: str = Field(min_length=1, max_length=2000)
    tone: Literal["Professional", "Friendly", "Concise"]


class PrepareRequest(RequestModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=20000)


class SendRequest(RequestModel):
    expected_message_id: str = Field(min_length=1, max_length=1000)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_boolean_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean confirmation is required.")
        return value


class IngestRequest(RequestModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(ge=1, le=100)
    mode: Literal["ai", "demo"]


class DigestRequest(RequestModel):
    account: str = Field(min_length=3, max_length=320)
    day: date
    regenerate: bool = False


class JobResponse(BaseModel):
    id: str
    kind: Literal["ingest", "draft_reply", "daily_digest", "scheduled_triage"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    max_attempts: int
    available_at: float
    worker_id: str | None
    cancel_requested: bool
    error: str | None
    request_id: str | None
    created: str
    started: str | None
    finished: str | None
    target_row_id: int | None
    progress: dict | None
    result: dict | None = None


class AutomationRequest(RequestModel):
    interval_minutes: int = Field(ge=1, le=1440)
    batch_size: int = Field(ge=1, le=100)
    daily_limit: int = Field(ge=1, le=1000)
    query: str = Field(min_length=1, max_length=500)
    mode: Literal["ai", "demo"]
    auto_labels: list[Label]
    suggested_reply_labels: list[Label]
    reply_tone: Literal["Professional", "Friendly", "Concise"]
    reply_instruction: str = Field(min_length=1, max_length=2000)
    digest_enabled: bool
    digest_hour: int = Field(ge=0, le=23)
    digest_minute: int = Field(ge=0, le=59)


class SummaryResponse(BaseModel):
    total: int
    pending: int
    applied: int
    backend: Literal["sqlite", "postgresql"]


class EmailResponse(BaseModel):
    id: int
    source: Literal["gmail", "sample"]
    account: str
    email_id: str
    subject: str
    sender: str
    label: Label
    reason: str
    needs_review: bool
    mode: Literal["ai", "demo"]
    status: Literal["pending", "approved", "rejected", "applied", "undone"]
    created: str


class EmailPageResponse(BaseModel):
    items: list[EmailResponse]
    total: int
    pages: int
    page: int


class EmailDetailsResponse(BaseModel):
    email: EmailResponse
    proposal: dict | None
    decisions: list[dict]
    reply: dict | None
    suggestion: dict | None
