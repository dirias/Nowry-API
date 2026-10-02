"""
GTM-005 — two bugs in paid features (docs/prd-road-to-market.md FR-009).

1. Book indexing stores the Mongo user id, the id chat retrieval filters on.
   Before this, indexing wrote the Firebase uid and `retrieve_book_context`
   never matched a chunk, so Knowledge Access over a book returned nothing.
2. The Anki import quota fails closed: a failure to read the plan is a 503 the
   client can retry, not an unlimited import. It also counts only live cards.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from fastapi import HTTPException

OWNER = "507f1f77bcf86cd799439011"


def test_book_indexing_and_chat_retrieval_use_the_same_user_id():
    """Read as text: importing the agent router needs the real models package."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    books_source = (app_dir / "routers" / "books.py").read_text()
    agent_source = (app_dir / "routers" / "agent.py").read_text()

    indexing = books_source[books_source.index("from app.utils.book_rag import index_book") :][:600]
    assert 'user_id: str = current_user["user_id"]' in indexing
    assert 'current_user["uid"]' not in indexing

    retrieval = agent_source[agent_source.index("rag_book_context = await retrieve_book_context(") :][:200]
    assert "user_id=user_id," in retrieval
    chat_head = agent_source[agent_source.index("async def chat(") :][:1200]
    assert 'user_id = current_user.get("user_id")' in chat_head


@pytest.mark.asyncio
async def test_quota_fails_closed_when_the_plan_cannot_be_read():
    from app.routers import import_apkg

    users = MagicMock()
    users.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    with patch.object(import_apkg, "users_collection", users):
        with pytest.raises(HTTPException) as raised:
            await import_apkg._get_remaining_quota(OWNER)
    assert raised.value.status_code == 503
    assert raised.value.detail == {"code": import_apkg.QUOTA_UNAVAILABLE_CODE}


@pytest.mark.asyncio
async def test_quota_counts_only_live_cards():
    from app.routers import import_apkg

    users = MagicMock()
    users.find_one = AsyncMock(return_value={"_id": OWNER, "subscription": {"tier": "free"}})
    cards = MagicMock()
    cards.count_documents = AsyncMock(return_value=30)
    with patch.object(import_apkg, "users_collection", users), patch.object(import_apkg, "cards_collection", cards):
        remaining = await import_apkg._get_remaining_quota(OWNER)
    assert remaining == 20  # free plan: 50 flashcards
    assert cards.count_documents.call_args[0][0] == {"user_id": OWNER, "deleted_at": None}


@pytest.mark.asyncio
async def test_quota_is_unlimited_for_pro_without_counting():
    from app.routers import import_apkg

    users = MagicMock()
    users.find_one = AsyncMock(return_value={"_id": OWNER, "subscription": {"tier": "pro"}})
    cards = MagicMock()
    cards.count_documents = AsyncMock(return_value=0)
    with patch.object(import_apkg, "users_collection", users), patch.object(import_apkg, "cards_collection", cards):
        assert await import_apkg._get_remaining_quota(OWNER) == -1
    cards.count_documents.assert_not_awaited()
