"""
The public contact form (docs/prd-public-site.md D6, ADR-035 §6).

A visitor writes before they have an account, so nothing here carries a user
id. The bounds are the form's: a name that fits a line, a message long enough
to be one and short enough to be read.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class ContactMessageCreate(BaseModel):
    """What the form posts."""

    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    message: str = Field(min_length=10, max_length=4000)
    locale: Optional[str] = Field(default=None, min_length=2, max_length=5)
    page: Optional[str] = Field(default=None, max_length=200)


class ContactMessageResponse(BaseModel):
    """The acknowledgement the form shows."""

    message_id: str
    message: str


class ContactMessage(BaseModel):
    """The stored document, for whoever reads the collection."""

    name: str
    email: str
    message: str
    locale: Optional[str] = None
    page: Optional[str] = None
    status: str = "new"
    created_at: datetime
