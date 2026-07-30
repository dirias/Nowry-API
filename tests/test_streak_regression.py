"""
Phase 31 — D-08 regression: Browse/Cram sessions must never move the streak.

Verification test only (no production code change). `review_card` in
study_cards.py is the SOLE writer of `last_reviewed`, and Browse/Cram never
call it (Phase 30's structural guarantee). This test locks in that a session
with no `last_reviewed` writes in the reviewed-cards window computes a
streak of 0 in app.routers.users.get_user_stats.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Python 3.9 compatibility stubs — mirrors the sys.modules-stub-before-router-
# import idiom established in test_smart_pet.py, sized to what
# app.routers.users actually imports transitively:
#   - bcrypt: real dependency (requirements.txt), not installed in this local
#     Python 3.9 dev/test venv (same category as slowapi/langfuse elsewhere).
#   - app.auth.firebase_auth: uses Python 3.10+ `dict | None` syntax and fails
#     to import on Python 3.9 (same known issue documented for other routers).
for mod in ["bcrypt"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

if "app.auth.firebase_auth" not in sys.modules:
    _mock_firebase_auth_mod = MagicMock()
    _mock_firebase_auth_mod.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock_firebase_auth_mod

import pytest


@pytest.mark.asyncio
async def test_streak_unaffected_when_last_reviewed_untouched(mock_users_collection):
    """D-08 regression: a Browse/Cram session that wrote no last_reviewed
    timestamps must not extend/create a streak — get_user_stats returns
    study_streak == 0 when the reviewed-cards cursor yields nothing."""
    from app.routers.users import get_user_stats

    mock_books_collection = MagicMock()
    mock_books_collection.count_documents = AsyncMock(return_value=0)

    mock_study_cards_collection = MagicMock()
    mock_study_cards_collection.count_documents = AsyncMock(return_value=0)
    mock_agg_cursor = MagicMock()
    mock_agg_cursor.to_list = AsyncMock(return_value=[])  # no distinct reviewed dates this window
    mock_study_cards_collection.aggregate = MagicMock(return_value=mock_agg_cursor)

    with patch("app.routers.users.study_cards_collection", mock_study_cards_collection), \
         patch("app.routers.users.books_collection", mock_books_collection), \
         patch("app.routers.users.users_collection", mock_users_collection):
        stats = await get_user_stats("507f1f77bcf86cd799439011")

    assert stats["study_streak"] == 0


@pytest.mark.asyncio
async def test_streak_stays_zero_across_multiple_stat_queries(mock_users_collection):
    """Behavioral invariant, not a source-level check (per plan note): even
    though get_user_stats issues several independent count_documents calls
    (total/flashcards/reviewed/quiz/visual) alongside the streak cursor, a
    completely untouched review window still yields study_streak == 0 —
    documenting that Browse/Cram's lack of last_reviewed writes is the sole
    input the streak calculation depends on."""
    from app.routers.users import get_user_stats

    mock_books_collection = MagicMock()
    mock_books_collection.count_documents = AsyncMock(return_value=0)

    mock_study_cards_collection = MagicMock()
    # Non-zero counts for unrelated stats (total_cards, flashcards_count, etc.)
    # must not influence the streak calculation, which is driven solely by
    # the separate find()->cursor path below.
    mock_study_cards_collection.count_documents = AsyncMock(return_value=42)
    mock_agg_cursor = MagicMock()
    mock_agg_cursor.to_list = AsyncMock(return_value=[])
    mock_study_cards_collection.aggregate = MagicMock(return_value=mock_agg_cursor)

    with patch("app.routers.users.study_cards_collection", mock_study_cards_collection), \
         patch("app.routers.users.books_collection", mock_books_collection), \
         patch("app.routers.users.users_collection", mock_users_collection):
        stats = await get_user_stats("507f1f77bcf86cd799439011")

    assert stats["study_streak"] == 0
    assert stats["total_cards"] == 42
