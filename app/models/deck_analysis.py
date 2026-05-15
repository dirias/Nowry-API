from __future__ import annotations

from pydantic import BaseModel


class DuplicatePair(BaseModel):
    card_a_id: str
    card_b_id: str
    reason: str


class TopicGap(BaseModel):
    topic: str
    description: str


class RewriteSuggestion(BaseModel):
    card_id: str
    original_front: str
    original_back: str
    suggested_front: str
    suggested_back: str
    reason: str


class DeckAnalysisRequest(BaseModel):
    deck_id: str


class DeckAnalysisResponse(BaseModel):
    duplicates: list[DuplicatePair]
    gaps: list[TopicGap]
    rewrite_suggestions: list[RewriteSuggestion]
