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


def resolve_deck_budget(deck_doc: Optional[dict]) -> tuple["PaceMode", int, int]:
    """Single source of truth for a deck's daily budget.

    Returns (pace_mode, new_per_day, max_reviews_per_day). Falls back to the
    balanced defaults when the deck has no config or stores an unknown mode.
    Used by both the decks router (dashboard counts) and the study_cards
    router (session selection) so they cannot drift.
    """
    cfg = (deck_doc or {}).get("config") or {}
    mode_str = cfg.get("pace_mode", PaceMode.balanced.value)
    try:
        mode = PaceMode(mode_str)
    except ValueError:
        mode = PaceMode.balanced
    defaults = PACE_DEFAULTS[mode.value]
    new_per_day = cfg.get("new_per_day") or defaults["new_per_day"]
    max_reviews = cfg.get("max_reviews_per_day") or defaults["max_reviews_per_day"]
    return mode, int(new_per_day), int(max_reviews)


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
