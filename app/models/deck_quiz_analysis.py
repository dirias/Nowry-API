from __future__ import annotations

from pydantic import BaseModel


class DeckQuizAnalysisResponse(BaseModel):
    quiz_questions: list[str]
    card_count: int
    deck_id: str
