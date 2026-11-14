"""
What the moderation routes answer and take (ADMIN-002, ADMIN-003; docs/prd-admin.md FR-002, FR-004).

Every id leaves as a string. The reporter's own list never names the reporter;
the admin list does, the queue does not (a learner's report stays theirs).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field

ReportReason = Literal["copyright", "inappropriate", "spam", "misinformation", "other"]
ReportStatus = Literal["pending", "under_review", "resolved", "dismissed"]
ReportedType = Literal["book", "deck", "card"]
RemovableType = Literal["deck", "book"]
QueueState = Literal["to_review", "kept", "removed"]

#: The longest note an admin can add to a removal (FR-004).
NOTE_MAX_LENGTH: int = 500


class ReportCreated(BaseModel):
    message: str
    report_id: str


class ReportView(BaseModel):
    """One report as its reporter sees it."""

    id: str
    content_type: ReportedType
    content_id: str
    content_title: Optional[str] = None
    reason: ReportReason
    description: Optional[str] = None
    status: ReportStatus
    action_taken: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AdminReportView(ReportView):
    """One report as the admin sees it: who reported it and who reviewed it."""

    reporter_user_id: Optional[str] = None
    reporter_email: Optional[str] = None
    reviewed_by: Optional[str] = None
    resolution_notes: Optional[str] = None


class ReportPage(BaseModel):
    items: List[ReportView]
    total: int
    page: int
    page_size: int
    total_pages: int


class AdminReportPage(BaseModel):
    items: List[AdminReportView]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── The Moderation tab (ADMIN-003) ──────────────────────────────────────────


class RemovalRequest(BaseModel):
    reason: ReportReason
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX_LENGTH)


class ReviewRequest(BaseModel):
    """`kept` dismisses the item's open reports; `removed` unpublishes it (ADR-087).

    `action_taken` is the field's old name and is still read. A removal's
    reason defaults to the report's own.
    """

    action: Literal["kept", "removed"] = Field(validation_alias=AliasChoices("action", "action_taken"))
    reason: Optional[ReportReason] = None
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX_LENGTH)


class QueueReport(BaseModel):
    """A report inside a queue item: no reporter address, no reporter id."""

    id: str
    reason: ReportReason
    note: Optional[str] = None
    created_at: Optional[datetime] = None


class RemovalView(BaseModel):
    removed_at: Optional[datetime] = None
    reason: Optional[ReportReason] = None
    note: Optional[str] = None


class QueueItem(BaseModel):
    content_type: RemovableType
    content_id: str
    title: str
    owner_username: Optional[str] = None
    is_public: bool
    public_since: Optional[datetime] = None
    size: int
    learners: int
    public_copies: int
    reports: List[QueueReport]
    report_count: int
    oldest_report_at: Optional[datetime] = None
    removal: Optional[RemovalView] = None


class QueueTotals(BaseModel):
    to_review: int
    kept: int
    removed: int


class QueueResponse(BaseModel):
    items: List[QueueItem]
    totals: QueueTotals


class KeepResult(BaseModel):
    dismissed: int


class RemovedItem(BaseModel):
    content_type: RemovableType
    content_id: str
    title: str


class RemovalResult(BaseModel):
    removed: List[RemovedItem]
    copies_removed: int
    owners_told: int
    owners_not_told: int


class ReviewResult(BaseModel):
    """The review's answer: `dismissed` for `kept`, `removal` for `removed`."""

    report_id: str
    action: Literal["kept", "removed"]
    dismissed: Optional[int] = None
    removal: Optional[RemovalResult] = None
