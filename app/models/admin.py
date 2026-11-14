"""
The Overview's and the admin log's wire shapes (ADMIN-007c, docs/prd-admin.md FR-011, boards A, K).

A log row names its actor by username, never by address; its details are the
safe subset `app/services/admin_log.safe_details` keeps.
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

#: The log's filters; each covers a fixed set of actions (`admin_log.KINDS`).
ActionKind = Literal["invitations", "moderation", "bugs", "access"]


class ActionActor(BaseModel):
    kind: Literal["admin", "script"]
    username: Optional[str] = None
    is_you: bool = False


class ActionRow(BaseModel):
    id: str
    at: datetime
    actor: ActionActor
    action: str
    target_type: str
    target_id: Optional[str] = None
    target_title: Optional[str] = None
    reason: Optional[str] = None
    details: Dict[str, Any] = {}


class ActionsPage(BaseModel):
    actions: List[ActionRow]
    #: Pass as `before` for the next page; null when this page is the last.
    next_before: Optional[datetime] = None


class ReportsToReview(BaseModel):
    count: int
    oldest_at: Optional[datetime] = None
    reasons: List[str]


class NewBugs(BaseModel):
    count: int
    critical: int


class InvitationsEnding(BaseModel):
    count: int
    cohorts: List[str]


class Waiting(BaseModel):
    count: int
    since_last_batch: int


class OverviewNeeds(BaseModel):
    reports_to_review: ReportsToReview
    new_bugs: NewBugs
    invitations_ending: InvitationsEnding
    waiting: Waiting


class EmailMeter(BaseModel):
    sent: int
    share: int


class EmailsToday(BaseModel):
    invitations: EmailMeter
    confirmations: EmailMeter
    resets_at: datetime


class OverviewResponse(BaseModel):
    needs: OverviewNeeds
    emails_today: EmailsToday
    recent_actions: List[ActionRow]
