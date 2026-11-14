"""
The beta gate's wire shapes (ADR-038, docs/prd-road-to-market.md FR-001..FR-004).

Nothing here carries a user id: the config is public, an invite is checked
before an account exists, and a waitlist entry is written by someone who was
not let in.
"""
from datetime import datetime
from typing import Annotated, Dict, List, Literal, Optional

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
InviteLocale = Literal["en", "es", "fr", "de", "ja"]
#: A cohort name: lowercase letters, digits and hyphens, up to 40.
COHORT_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class InviteCreate(BaseModel):
    """What the operator posts to invite one address."""

    email: EmailStr
    cohort: str = Field(min_length=1, max_length=40, pattern=COHORT_PATTERN)
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
    # The letter (INVITE-008): its language, when it last left, how many times, and why the last try did not.
    locale: Optional[str] = None
    sent_at: Optional[datetime] = None
    send_count: int = 0
    last_error: Optional[str] = None
    # How long a link made for this row lasts; a resend makes the new one as long (INVITE-009).
    lifetime_days: int = 14


class InviteCreated(InviteSummary):
    """The one answer that carries the link. It cannot be read again."""

    link: str


class InviteListResponse(BaseModel):
    invites: List[InviteSummary]
    counts: Dict[str, Dict[str, int]]
    #: Every invitation by state, for the list's filter chips.
    totals: Dict[InviteState, int]


class InviteBatchRequest(BaseModel):
    """Up to 50 pasted entries, already split by the client; checked first (`dry_run`), then sent."""

    addresses: List[Annotated[str, Field(max_length=320)]] = Field(min_length=1, max_length=50)
    cohort: str = Field(min_length=1, max_length=40, pattern=COHORT_PATTERN)
    expires_in_days: int = Field(default=14, ge=1, le=90)
    locale: InviteLocale = "en"
    dry_run: bool = True


InviteBatchOutcome = Literal["will_invite", "open_invite", "has_account", "invalid", "left_list", "sent", "send_failed"]


class InviteBatchResult(BaseModel):
    """One distinct entry and what happened to it. Never a link."""

    input: str
    email: Optional[str] = None
    outcome: InviteBatchOutcome
    invite_id: Optional[str] = None


class InviteBatchResponse(BaseModel):
    dry_run: bool
    results: List[InviteBatchResult]
    counts: Dict[str, int]
    invitations_left_today: int


class InviteCohortsResponse(BaseModel):
    """What the paste form offers: cohorts in use, link lifetimes, and today's invitations left."""

    cohorts: List[str]
    default_cohort: str
    lifetimes: List[int]
    default_lifetime: int
    invitations_left_today: int
