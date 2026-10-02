"""
The beta gate's wire shapes (ADR-038, docs/prd-road-to-market.md FR-001..FR-004).

Nothing here carries a user id: the config is public, an invite is checked
before an account exists, and a waitlist entry is written by someone who was
not let in.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class BetaConfigResponse(BaseModel):
    """What every client reads once to decide what to show."""

    active: bool
    invite_required: bool
    upgrades_open: bool


class InviteCheckRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class InviteCheckResponse(BaseModel):
    valid: bool
    cohort: Optional[str] = None


class WaitlistCreate(BaseModel):
    """What the waitlist form posts."""

    email: EmailStr
    locale: Optional[str] = Field(default=None, min_length=2, max_length=5)
    source: Optional[str] = Field(default=None, max_length=200)


class WaitlistResponse(BaseModel):
    message: str


class WaitlistEntry(BaseModel):
    """The stored row, one per email, for whoever invites the next cohort."""

    email: str
    locale: Optional[str] = None
    source: Optional[str] = None
    status: str = "waiting"
    created_at: datetime
    updated_at: datetime
