"""
Phase 33 Plan 04 — get_statistics equivalence + BOLA $match-scoping tests.

RESEARCH.md Pattern 4: `get_statistics` (study_cards.py) replaces a
`to_list(length=2000)` fetch + nested Python day-bucketing/streak loop with a
MongoDB aggregation pipeline (copying the `get_card_tags` $match-first
pattern). This test is written against the POST-fix aggregation shape and is
EXPECTED TO FAIL (RED) until Task 2 lands the aggregation pipeline in
study_cards.py — `collection.aggregate(...)` is not called by `get_statistics`
yet, and `app.routers.study_cards.books_collection` does not exist as a
module-level name yet (it is a local import inside the function today), so
the `patch(...)` target in this file's fixtures will raise AttributeError
until Task 2 hoists it to module level.
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Python 3.9 compatibility stubs — mirrors the sys.modules-stub-before-router-
# import idiom established in test_streak_regression.py:
#   - bcrypt: real dependency (requirements.txt), not installed in this local
#     Python 3.9 dev/test venv (same category as slowapi/langfuse elsewhere).
#   - app.auth.firebase_auth: defensively stubbed in case any transitive
#     import in this module chain relies on Python 3.10+ syntax; matches the
#     established idiom for router-adjacent test files.
for mod in ["bcrypt"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

if "app.auth.firebase_auth" not in sys.modules:
    _mock_firebase_auth_mod = MagicMock()
    _mock_firebase_auth_mod.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock_firebase_auth_mod

import pytest

from app.routers.study_cards import get_statistics

USER_ID = "507f1f77bcf86cd799439011"


def _make_cursor(return_value):
    """A MagicMock standing in for a Motor aggregation cursor: only
    `.to_list(...)` needs to be awaitable."""
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=return_value)
    return cursor


def _make_mock_collection(grouped_rows, recent_cards, struggle_cards, counts):
    """Build a mock `cards_collection` covering every query get_statistics
    issues:
      - `.aggregate(pipeline)` -> weekly/streak grouped-day-count rows
      - `.find({..., "last_reviewed": {"$ne": None}}).sort(...).limit(10).to_list(...)`
        -> top-10 recent_performance record docs
      - `.find({..., "repetitions": {"$lte": 1}}).sort(...).to_list(...)`
        -> last_session_struggle candidate docs
      - `.count_documents(...)` -> total_cards / reviewed_cards / due_today,
        in call order, via `counts` (a 3-element list).
    """
    collection = MagicMock()

    collection.aggregate = MagicMock(return_value=_make_cursor(grouped_rows))

    recent_find_result = MagicMock()
    recent_find_result.sort.return_value.limit.return_value.to_list = AsyncMock(
        return_value=recent_cards
    )

    struggle_find_result = MagicMock()
    struggle_find_result.sort.return_value.to_list = AsyncMock(
        return_value=struggle_cards
    )

    def _find_side_effect(query, *args, **kwargs):
        if "repetitions" in query:
            return struggle_find_result
        return recent_find_result

    collection.find = MagicMock(side_effect=_find_side_effect)
    collection.count_documents = AsyncMock(side_effect=counts)

    return collection


@pytest.mark.asyncio
async def test_get_statistics_equivalence_shape():
    """weekly_progress/recent_performance/summary keep the current response
    shape and values when fed by the aggregation's grouped day/type rows."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    grouped_rows = [
        {"_id": {"day": today_str, "type": "flashcard"}, "count": 3},
        {"_id": {"day": today_str, "type": "quiz"}, "count": 1},
        {"_id": {"day": yesterday_str, "type": "flashcard"}, "count": 2},
    ]
    recent_cards = [
        {
            "title": "Card A",
            "ease_factor": 2.5,
            "card_type": "flashcard",
            "last_reviewed": now,
        }
    ]
    struggle_cards = []

    collection = _make_mock_collection(
        grouped_rows=grouped_rows,
        recent_cards=recent_cards,
        struggle_cards=struggle_cards,
        counts=[10, 6, 4],  # total_cards, reviewed_cards, due_today
    )

    with patch("app.routers.study_cards.books_collection") as mock_books_collection:
        mock_books_collection.find.return_value.to_list = AsyncMock(return_value=[])

        result = await get_statistics(
            collection=collection,
            current_user={"user_id": USER_ID},
        )

    assert "weekly_progress" in result
    assert "recent_performance" in result
    assert "summary" in result

    assert isinstance(result["weekly_progress"], list)
    assert len(result["weekly_progress"]) == 7
    # Last entry is always "today" (the loop walks 6 days ago -> today).
    assert result["weekly_progress"][-1]["date"] == today_str
    assert result["weekly_progress"][-1]["flashcards"] == 3
    assert result["weekly_progress"][-1]["quizzes"] == 1
    assert result["weekly_progress"][-1]["visual"] == 0

    assert any(rp.get("card_title") == "Card A" for rp in result["recent_performance"])

    summary = result["summary"]
    for key in (
        "total_cards",
        "reviewed_cards",
        "new_cards",
        "due_today",
        "current_streak",
        "last_session_struggle",
    ):
        assert key in summary
    assert summary["total_cards"] == 10
    assert summary["reviewed_cards"] == 6
    assert summary["new_cards"] == 4
    assert summary["due_today"] == 4
    # today + yesterday both have grouped rows -> 2-day streak, no gap.
    assert summary["current_streak"] == 2
    assert summary["last_session_struggle"] is None


@pytest.mark.asyncio
async def test_get_statistics_bola_match_scoping():
    """The aggregate pipeline's FIRST stage must be a $match scoped to the
    requesting user's own, non-deleted cards — the sole enforcement point
    for the aggregation (T-33-01)."""
    collection = _make_mock_collection(
        grouped_rows=[],
        recent_cards=[],
        struggle_cards=[],
        counts=[0, 0, 0],
    )

    with patch("app.routers.study_cards.books_collection") as mock_books_collection:
        mock_books_collection.find.return_value.to_list = AsyncMock(return_value=[])
        await get_statistics(collection=collection, current_user={"user_id": USER_ID})

    assert collection.aggregate.called, "get_statistics must call collection.aggregate(...)"
    pipeline_arg = collection.aggregate.call_args[0][0]
    assert isinstance(pipeline_arg, list) and len(pipeline_arg) > 0

    first_stage = pipeline_arg[0]
    assert "$match" in first_stage, "First pipeline stage must be a $match (BOLA enforcement point)"

    match_clause = first_stage["$match"]
    assert "user_id" in match_clause
    assert "deleted_at" in match_clause
    assert match_clause["user_id"] == USER_ID
    assert match_clause["deleted_at"] is None
