"""
Pydantic v2 models for the study session history feature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SessionCard(BaseModel):
    """
    One card/question from a completed session.

    AI quiz  → question_text, correct_answer, question_type, evaluation
    Deck quiz → card_id, evaluation  (card document is the source of truth)
    """
    # AI quiz fields
    question_text: str | None = None
    correct_answer: str | None = None
    question_type: str | None = None
    # Deck quiz field
    card_id: str | None = None
    # Both
    evaluation: str = "incorrect"


class LoggedCard(BaseModel):
    """One card graded during an SRS review session."""
    card_id: str
    card_title: str | None = None   # Front/title of the card for history display
    grade: str                      # "again" | "hard" | "good" | "easy"
    evaluation: str                 # "incorrect" | "partial" | "correct"


class LogSessionRequest(BaseModel):
    """
    Payload sent by the frontend when an SRS flashcard session completes.

    The frontend tracks all graded cards during the session and sends
    the full list on completion. The backend computes totals and persists
    the permanent record.
    """
    session_type: str = "srs_review"
    deck_id: str | None = None          # None for daily-review cross-deck sessions
    deck_name: str | None = None
    started_at: datetime
    cards: list[LoggedCard]


class StudySessionRecord(BaseModel):
    """One completed study session."""
    id: str
    session_type: str                   # "ai_quiz" | "deck_quiz"
    topic: str | None = None            # AI quiz: topic string
    deck_id: str | None = None          # Deck quiz: deck ObjectId string
    deck_name: str | None = None        # Deck quiz: human-readable name
    total_cards: int
    correct: int
    partial: int
    incorrect: int
    score_percentage: int
    duration_seconds: int
    cards: list[dict[str, Any]] = []    # Raw dicts — shape varies by session_type
    started_at: datetime
    completed_at: datetime


class StudySessionsResponse(BaseModel):
    sessions: list[StudySessionRecord]
    total: int                          # Total across all pages (for "View all N sessions")
