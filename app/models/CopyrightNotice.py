"""
Copyright notices from the takedown page (GTM-008, docs/prd-road-to-market.md FR-012).

A notice is sent by someone who is not a user, so nothing here carries a user
id. The five things a notice must contain are the five fields; the statement
is a checkbox that must be true, and the typed name is the signature.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class CopyrightNoticeCreate(BaseModel):
    """What the takedown form posts."""

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    work: str = Field(min_length=10, max_length=2000)
    location: str = Field(min_length=5, max_length=500)
    statement: bool
    signature: str = Field(min_length=1, max_length=120)
    locale: Optional[str] = Field(default=None, min_length=2, max_length=5)

    @field_validator("statement")
    @classmethod
    def _must_be_affirmed(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("the good-faith statement must be affirmed")
        return value


class CopyrightNoticeResponse(BaseModel):
    """The acknowledgement the form shows."""

    notice_id: str
    message: str


class CopyrightNotice(BaseModel):
    """The stored document, for whoever handles notices."""

    name: str
    email: str
    work: str
    location: str
    signature: str
    locale: Optional[str] = None
    status: str = "new"
    created_at: datetime
