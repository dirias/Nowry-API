"""
What the moderation routes answer and take (ADMIN-002, ADMIN-003; docs/prd-admin.md FR-002, FR-004).

Every id leaves as a string. The reporter's own list never names the reporter;
the admin list does, the queue does not (a learner's report stays theirs).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel

ReportReason = Literal["copyright", "inappropriate", "spam", "misinformation", "other"]
ReportStatus = Literal["pending", "under_review", "resolved", "dismissed"]
ReportedType = Literal["book", "deck", "card"]


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
