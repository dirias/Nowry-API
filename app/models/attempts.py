"""
Pydantic v2 models for the attempts ledger (ADR-059 §1).

One row per graded showing. The review endpoint writes it with the card's
state before the grade; a practice showing (ADR-057 §2) writes a row and
changes nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AttemptHelp(BaseModel):
    """What the learner reached for before grading (ADR-059 §2)."""
    companion: str | None = None        # kind of help asked for, or None
    audio: int = Field(default=0, ge=0, le=50)
    hint: bool = False
    source: bool = False


class AttemptBody(BaseModel):
    """
    Optional body on POST /study-cards/{id}/review: the attempt behind the grade.

    Every field is optional so an old client, or a review without a session,
    still lands as a row with what is known.
    """
    client_session_id: str | None = Field(default=None, max_length=64)
    showing: Literal["scheduled", "practice"] = "scheduled"
    side: Literal["front", "back"] | None = None
    think_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    judge_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    confidence: Literal["sure", "maybe", "none"] | None = None
    chosen_option: str | None = Field(default=None, max_length=500)
    help: AttemptHelp = AttemptHelp()
    position: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    scope: str | None = Field(default=None, max_length=20)
    block_id: str | None = Field(default=None, max_length=64)
    local_hour: int | None = Field(default=None, ge=0, le=23)
    client: Literal["web", "phone"] | None = None


class AttemptBefore(BaseModel):
    """The card's schedule as it stood when the card was shown."""
    interval: int = 1
    ease_factor: float = 2.5
    repetitions: int = 0
    lapses: int = 0
    days_overdue: int | None = None


class AttemptAfter(BaseModel):
    interval: int
    ease_factor: float


class AttemptRecord(BaseModel):
    """One row of the ledger, as a client reads it."""
    id: str
    card_id: str
    card_title: str | None = None
    deck_id: str | None = None
    tags: list[str] = []
    card_type: str = "flashcard"
    grade: str
    evaluation: str
    showing: str = "scheduled"
    side: str | None = None
    think_ms: int | None = None
    judge_ms: int | None = None
    confidence: str | None = None
    chosen_option: str | None = None
    help: AttemptHelp = AttemptHelp()
    before: AttemptBefore = AttemptBefore()
    after: AttemptAfter | None = None
    position: int | None = None
    total: int | None = None
    scope: str | None = None
    client: str | None = None
    graded_at: datetime


class SessionAttemptsResponse(BaseModel):
    attempts: list[AttemptRecord]
    median_think_ms: int | None = None      # the learner's own median for this deck (ADR-059 §3)
    days_since_previous: int | None = None  # how long before this session the previous one ended
