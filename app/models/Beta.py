"""
The beta gate's wire shapes (ADR-038, docs/prd-road-to-market.md FR-001..FR-004).

Nothing here carries a user id: the config is public, an invite is checked
before an account exists, and a waitlist entry is written by someone who was
not let in.
"""
from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class BetaConfigResponse(BaseModel):
    """What every client reads once to decide what to show."""

    active: bool
    invite_required: bool
    upgrades_open: bool


class InviteCheckRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class InviteCheckResponse(BaseModel):
    """`cohort` stays on the wire for earlier clients and is always null (ADR-077)."""

    valid: bool
    cohort: Optional[str] = None


class WaitlistCreate(BaseModel):
    """What the waitlist form posts."""

    email: EmailStr
    locale: Optional[str] = Field(default=None, min_length=2, max_length=5)
    source: Optional[str] = Field(default=None, max_length=200)


class WaitlistResponse(BaseModel):
    message: str


class WaitlistLeaveHint(BaseModel):
    """What the leave page may show: the address with all but its first letter hidden, and the row's language."""

    email_hint: Optional[str] = None
    locale: Optional[str] = None


class WaitlistLeft(BaseModel):
    """The one answer to a request to leave, whatever the token."""

    left: bool = True


class WaitlistEntry(BaseModel):
    """The stored row, one per email, for whoever invites the next cohort."""

    email: str
    locale: Optional[str] = None
    source: Optional[str] = None
    status: str = "waiting"
    created_at: datetime
    updated_at: datetime


# ── Personal invitations (ADR-077, FR-019) ───────────────────────────────────

InviteState = Literal["open", "used", "revoked", "expired"]


class InviteCreate(BaseModel):
    """What the operator posts to invite one address."""

    email: EmailStr
    cohort: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$")
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=90)
    note: Optional[str] = Field(default=None, max_length=200)


class InviteSummary(BaseModel):
    """An invitation as the operator reads it. No token and no hash."""

    id: str
    email: str
    cohort: str
    state: InviteState
    note: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class InviteCreated(InviteSummary):
    """The one answer that carries the link. It cannot be read again."""

    link: str


class InviteListResponse(BaseModel):
    invites: List[InviteSummary]
    counts: Dict[str, Dict[str, int]]
