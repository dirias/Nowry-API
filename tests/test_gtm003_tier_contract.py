"""
GTM-003 — the tier contract's free column and fair-use ceilings (ADR-041).

  1. The plan table: everything by hand unlimited, the import ceiling, the
     monthly AI ceiling, the document ceiling, read-aloud characters.
  2. track_ai_usage: the increment is conditional on being under the ceiling,
     a refused call is 429 with a code and is never counted, the window rolls
     at the month's end, and -1 means no ceiling.
  3. Books: no count cap; a document past the word ceiling is 413.
  4. Cards: the free taste is two cards a call on both text endpoints, and
     the three unguarded generation endpoints are rate-limited.
  5. Import: counted on imported cards only, against import_cards.
  6. Read-aloud: a conditional monthly character reservation, 429 past it.
  7. Companion: one free portrait, for life.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.config.subscription_plans import AI_USAGE_LIMITS, FREE_PER_CALL, SUBSCRIPTION_PLANS, SubscriptionTier, plan_limit

APP = Path(__file__).resolve().parents[1] / "app"
USER = "507f1f77bcf86cd799439011"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


# ── 1. The table ─────────────────────────────────────────────────────────────


def test_everything_by_hand_is_unlimited_on_every_tier():
    for tier in SubscriptionTier:
        limits = SUBSCRIPTION_PLANS[tier]["limits"]
        assert limits["books"] == -1 and limits["flashcards"] == -1 and limits["quiz_questions"] == -1 and limits["visual_diagrams"] == -1
        assert "pages_per_book" not in limits and "ai_generations_per_month" not in limits
        assert limits["document_words"] == 200_000


def test_the_free_taste_and_the_ceilings():
    assert FREE_PER_CALL == {"cards": 2, "quiz_questions": 5, "illustrations": 2}
    assert AI_USAGE_LIMITS[SubscriptionTier.FREE] == 50
    assert AI_USAGE_LIMITS[SubscriptionTier.PLUS] == 1000 and AI_USAGE_LIMITS[SubscriptionTier.PRO] == 1000
    assert plan_limit("free", "import_cards") == 2000 and plan_limit("pro", "import_cards") == -1
    assert plan_limit("free", "tts_chars_per_month") == 0 and plan_limit("plus", "tts_chars_per_month") == 1_000_000
    assert plan_limit("not-a-tier", "ai_calls_per_month") == 50


# ── 2. The monthly ceiling ───────────────────────────────────────────────────


def _users(sub, updated=None):
    users = MagicMock()
    users.find_one = AsyncMock(return_value={"_id": ObjectId(USER), "subscription": sub})
    users.update_one = AsyncMock()
    users.find_one_and_update = AsyncMock(return_value=updated)
    return users


@pytest.mark.asyncio
async def test_a_call_under_the_ceiling_is_counted_conditionally():
    from app.auth import dependencies

    users = _users({"tier": "free", "ai_usage_count": 3, "ai_usage_reset_date": datetime(2026, 11, 1, tzinfo=timezone.utc)},
                   updated={"_id": ObjectId(USER), "subscription": {"tier": "free", "ai_usage_count": 4}})
    with patch.object(dependencies, "users_collection", users):
        user = await dependencies.track_ai_usage({"user_id": USER})
    assert user["user_id"] == USER
    users.update_one.assert_not_awaited()  # window still open: no reset
    query = users.find_one_and_update.call_args[0][0]
    assert query["$or"][0] == {"subscription.ai_usage_count": {"$lt": 50}}


@pytest.mark.asyncio
async def test_a_call_past_the_ceiling_is_refused_and_not_counted():
    from app.auth import dependencies

    users = _users({"tier": "free", "ai_usage_count": 50, "ai_usage_reset_date": datetime(2026, 11, 1, tzinfo=timezone.utc)}, updated=None)
    with patch.object(dependencies, "users_collection", users):
        with pytest.raises(HTTPException) as raised:
            await dependencies.track_ai_usage({"user_id": USER})
    assert raised.value.status_code == 429
    assert raised.value.detail["code"] == dependencies.AI_LIMIT_REACHED_CODE
    assert raised.value.detail["limit"] == 50
    assert raised.value.detail["resets_at"].startswith("2026-11-01")


@pytest.mark.asyncio
async def test_the_window_rolls_when_its_end_has_passed():
    from app.auth import dependencies

    users = _users({"tier": "free", "ai_usage_count": 50, "ai_usage_reset_date": datetime(2026, 9, 1, tzinfo=timezone.utc)},
                   updated={"_id": ObjectId(USER), "subscription": {"tier": "free", "ai_usage_count": 1}})
    with patch.object(dependencies, "users_collection", users):
        await dependencies.track_ai_usage({"user_id": USER})
    reset = users.update_one.call_args[0][1]["$set"]
    assert reset["subscription.ai_usage_count"] == 0
    assert reset["subscription.ai_usage_reset_date"].day == 1


@pytest.mark.asyncio
async def test_no_ceiling_means_no_condition():
    from app.auth import dependencies

    users = _users({"tier": "pro", "ai_usage_count": 5000, "ai_usage_reset_date": datetime(2026, 11, 1, tzinfo=timezone.utc)},
                   updated={"_id": ObjectId(USER), "subscription": {"tier": "pro"}})
    with patch.object(dependencies, "users_collection", users), patch.dict(dependencies.__dict__, {}):
        with patch("app.config.subscription_plans.AI_USAGE_LIMITS", {**AI_USAGE_LIMITS, SubscriptionTier.PRO: -1}):
            await dependencies.track_ai_usage({"user_id": USER})
    assert "$or" not in users.find_one_and_update.call_args[0][0]


# ── 3. Books ─────────────────────────────────────────────────────────────────


def test_no_book_count_cap_remains():
    source = (APP / "routers" / "books.py").read_text()
    assert "Book limit reached" not in source
    assert source.count("ensure_document_within_ceiling(stats[\"word_count\"])") == 2


def test_a_document_past_the_ceiling_is_413():
    from app.routers.books import DOCUMENT_TOO_LONG_CODE, ensure_document_within_ceiling

    ensure_document_within_ceiling(200_000)
    with pytest.raises(HTTPException) as raised:
        ensure_document_within_ceiling(200_001)
    assert raised.value.status_code == 413
    assert raised.value.detail == {"code": DOCUMENT_TOO_LONG_CODE, "limit": 200_000, "words": 200_001}


# ── 4. Cards ─────────────────────────────────────────────────────────────────


def test_free_taste_and_rate_limits_on_generation_endpoints():
    cards = (APP / "routers" / "cards.py").read_text()
    assert cards.count('effective_cap = min(effective_cap, FREE_PER_CALL["cards"])') == 2
    assert cards.count('@limiter.limit("10/minute")') == 2
    assert '@limiter.limit("10/minute")' in (APP / "routers" / "illustrations.py").read_text()
    assert '@limiter.limit("10/minute")' in (APP / "routers" / "visualizer.py").read_text()


# ── 5. Import ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_quota_counts_imported_cards_against_the_import_ceiling():
    from app.routers import import_apkg

    users = MagicMock(); users.find_one = AsyncMock(return_value={"_id": USER, "subscription": {"tier": "free"}})
    cards = MagicMock(); cards.count_documents = AsyncMock(return_value=1500)
    with patch.object(import_apkg, "users_collection", users), patch.object(import_apkg, "cards_collection", cards):
        assert await import_apkg._get_remaining_quota(USER) == 500
    assert cards.count_documents.call_args[0][0] == {"user_id": USER, "deleted_at": None, "source": "imported"}
    assert '"source": "imported"' in (APP / "routers" / "import_apkg.py").read_text()


# ── 6. Read-aloud ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tts_reservation_refuses_past_the_ceiling():
    from app.routers import tts

    users = MagicMock(); users.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
    with patch.object(tts, "users_collection", users):
        with pytest.raises(HTTPException) as raised:
            await tts.reserve_tts_characters(USER, 5000, 1_000_000, now=NOW)
    assert raised.value.status_code == 429
    assert raised.value.detail == {"code": tts.TTS_LIMIT_REACHED_CODE, "limit": 1_000_000}
    query = users.update_one.call_args[0][0]
    assert {"subscription.tts_chars_month": {"$lte": 995000}} in query["$or"]
    assert {"subscription.tts_chars_reset": {"$ne": "2026-10"}} in query["$or"]


@pytest.mark.asyncio
async def test_tts_reservation_is_skipped_without_a_ceiling():
    from app.routers import tts

    users = MagicMock(); users.update_one = AsyncMock()
    with patch.object(tts, "users_collection", users):
        await tts.reserve_tts_characters(USER, 5000, -1)
    users.update_one.assert_not_awaited()


# ── 7. Companion ─────────────────────────────────────────────────────────────


def test_free_gets_one_portrait_for_life():
    source = (APP / "routers" / "agent.py").read_text()
    block = source[source.index("async def generate_avatar(") :][:2500]
    assert 'if pet_now.get("avatar_url") or pet_now.get("stage_avatars"):' in block
    assert "SubscriptionTier.FREE: 1" in block
