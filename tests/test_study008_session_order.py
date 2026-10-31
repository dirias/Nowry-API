"""STUDY-008 — a session serves reviews first, most overdue first, then new cards (ADR-057 §1)."""
import inspect
from datetime import datetime

from app.routers import study_cards
from app.routers.study_cards import _session_order

NEW = [{"_id": "n1"}, {"_id": "n2"}]
REVIEWS = [
    {"_id": "r-recent", "next_review": datetime(2026, 10, 31)},
    {"_id": "r-oldest", "next_review": datetime(2026, 10, 1)},
    {"_id": "r-middle", "next_review": datetime(2026, 10, 15)},
]


def ids(cards: list) -> list:
    return [c["_id"] for c in cards]


def test_reviews_come_before_new_cards_most_overdue_first() -> None:
    assert ids(_session_order(NEW, REVIEWS)) == ["r-oldest", "r-middle", "r-recent", "n1", "n2"]


def test_new_cards_keep_their_order() -> None:
    assert ids(_session_order(NEW, [])) == ["n1", "n2"]


def test_a_review_without_a_date_does_not_break_the_order() -> None:
    assert ids(_session_order([], [{"_id": "x", "next_review": None}, REVIEWS[0]])) == ["x", "r-recent"]


def test_both_session_routes_use_the_order() -> None:
    source = inspect.getsource(study_cards)
    assert "cards = _session_order(all_new, all_review)" in source
    assert "cards = _session_order(new_cards_raw, review_cards_raw)" in source
    assert "new_cards_raw + review_cards_raw" not in source and "all_new + all_review" not in source
