from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class PaceMode(str, Enum):
    relaxed = "relaxed"
    balanced = "balanced"
    intensive = "intensive"


PACE_DEFAULTS: dict[str, dict[str, int]] = {
    "relaxed": {"new_per_day": 10, "max_reviews_per_day": 50},
    "balanced": {"new_per_day": 20, "max_reviews_per_day": 100},
    "intensive": {"new_per_day": 40, "max_reviews_per_day": 200},
}


class DeckConfigUpdate(BaseModel):
    pace_mode: PaceMode = PaceMode.balanced
    new_per_day: Optional[int] = Field(None, ge=1, le=500)
    max_reviews_per_day: Optional[int] = Field(None, ge=1, le=1000)


class DeckConfigResponse(BaseModel):
    deck_id: str
    pace_mode: PaceMode
    new_per_day: int
    max_reviews_per_day: int
    introduced_count: int
    total_cards: int
    introduced_pct: float
    estimated_completion_days: Optional[int]


class DailyBudgetResponse(BaseModel):
    deck_id: str
    new_cards_today: int
    reviews_today: int
    total_today: int
    budget_reached: bool
    new_per_day_limit: int
    reviews_per_day_limit: int


class DailySummaryResponse(BaseModel):
    total_today: int
    new_today: int
    reviews_today: int
    decks_with_work: int
    all_done: bool
