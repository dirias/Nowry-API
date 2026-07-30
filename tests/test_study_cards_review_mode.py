"""
Phase 32 Plan 02 — Browse/Cram SM-2 mode-guard integration tests (D-04/D-05/T-32-04).

Verifies POST /study-cards/{id}/review rejects non-study modes (browse, cram)
with 403 before any SM-2 mutation, while mode=study (and mode omitted, for
backward compatibility) still update the card's SM-2 fields.

Pytest-native (@pytest.mark.asyncio + app.dependency_overrides + httpx.ASGITransport),
collected by the normal `pytest tests/` run.

Deviation from the plan's literal `from app.main import app` instruction
(mirrored from test_deck_fixes.py): importing the *real* app.main in this
Python 3.9 dev venv transitively hits several pre-existing, unrelated
breakages (app/routers/auth.py's `str | None` PEP 604 syntax requires 3.10+;
app/routers/users.py needs `bcrypt`; app.ai_orchestrator.orchestrator needs
`langfuse.langchain`; app.routers.agent needs `google.generativeai.types`) —
confirmed test_deck_fixes.py's identical `from app.main import app` already
fails to collect today, independent of this plan's changes. Rule 3
(auto-fix blocking issue): instead of dragging in the whole app, this test
mounts a minimal FastAPI app with only `study_cards.router` — the exact
router under test — and drives it through the same real HTTP layer
(httpx.ASGITransport + app.dependency_overrides), so FastAPI's own
Query-param default resolution genuinely exercises the mode="study"
backward-compat default. `app.routers.agent` (needed only for review_card's
local `from app.routers.agent import grant_xp` XP side-effect) is stubbed in
sys.modules the same way test_tracing.py stubs heavy SDK-backed modules on
this test runner. See 32-02-SUMMARY.md.

Expected RED (browse/cram cases only) until Task 2 adds the `mode` query
param and the non-study 403 guard to review_card.
"""
from __future__ import annotations

import sys
import pytest
import httpx
from fastapi import FastAPI
from unittest.mock import MagicMock, AsyncMock
from bson import ObjectId

# app.routers.agent pulls in google.generativeai.types (not installed in this
# Python 3.9 dev venv) purely for AI-orchestration code unrelated to
# review_card's local `from app.routers.agent import grant_xp` XP
# side-effect. Stub the module in sys.modules so that lazy import resolves
# to a lightweight AsyncMock without dragging in the real, heavy module.
_mock_agent_module = MagicMock()
_mock_agent_module.grant_xp = AsyncMock(
    return_value={
        "level_up": False,
        "new_level": 1,
        "new_stage": 1,
        "avatar_regen_pending": False,
    }
)
sys.modules.setdefault("app.routers.agent", _mock_agent_module)

from app.routers import study_cards

# NOTE: do NOT `from app.auth.firebase_auth import get_firebase_user` here and use
# that as the dependency_overrides key. FastAPI's dependency_overrides dict keys on
# object identity, not behavior. Several other test files stub the entire
# app.auth.firebase_auth module in sys.modules at collection time (before any test
# function runs); when the whole suite runs together, a fresh import here can bind
# to a *different* get_firebase_user object than the one study_cards.py's own
# `Depends(get_firebase_user)` already captured when IT was first imported (from
# app.auth.firebase_auth import get_firebase_user, at study_cards.py's own module
# top) — so the override silently never intercepts, the real/stubbed dependency
# runs unmocked, and every request 422s. Reuse study_cards' own already-resolved
# reference instead (same fix pattern already correctly used one line below for
# study_cards.get_cards_collection) so the override key is always identical to
# whatever the router actually captured, regardless of collection order or what
# other files stubbed.

CARD_ID = "507f1f77bcf86cd799439099"
OWNER_USER_ID = "507f1f77bcf86cd799439011"  # matches conftest.mock_firebase_user

# Minimal app hosting only the router under test — avoids pulling in
# app.main's full router set (and its pre-existing, unrelated import breaks).
_test_app = FastAPI()
_test_app.include_router(study_cards.router)


async def _mock_owner_user():
    return {
        "user_id": OWNER_USER_ID,
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
    }


def _make_card_doc():
    """Fresh SM-2 doc per call — require_ownership mutates `_id`/`id` in place,
    so each test/request needs its own copy."""
    return {
        "_id": ObjectId(CARD_ID),
        "user_id": OWNER_USER_ID,
        "front": "Q",
        "back": "A",
        "ease_factor": 2.5,
        "interval": 1,
        "repetitions": 0,
        "last_reviewed": None,
    }


def _make_cards_collection(card_doc):
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=card_doc)
    collection.update_one = AsyncMock(return_value=None)
    return collection


async def _post_review(query_suffix: str, card_doc: dict):
    """POST /study-cards/{CARD_ID}/review?grade=good{query_suffix}.

    Overrides get_firebase_user (owner identity) and get_cards_collection
    (ownership resolution + SM-2 mutation target) so the request never
    touches real MongoDB.
    """
    collection = _make_cards_collection(card_doc)
    _test_app.dependency_overrides[study_cards.get_firebase_user] = _mock_owner_user
    _test_app.dependency_overrides[study_cards.get_cards_collection] = lambda: collection

    try:
        transport = httpx.ASGITransport(app=_test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            url = f"/study-cards/{CARD_ID}/review?grade=good{query_suffix}"
            response = await client.post(url)
    finally:
        _test_app.dependency_overrides.clear()

    return response, collection


@pytest.mark.asyncio
async def test_review_mode_browse_forbidden_no_mutation():
    """D-04/D-05: mode=browse is rejected with 403 before any SM-2 write."""
    card_doc = _make_card_doc()
    response, collection = await _post_review("&mode=browse", card_doc)
    assert response.status_code == 403
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_mode_cram_forbidden_no_mutation():
    """D-04/D-05: mode=cram is rejected with 403 before any SM-2 write."""
    card_doc = _make_card_doc()
    response, collection = await _post_review("&mode=cram", card_doc)
    assert response.status_code == 403
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_mode_study_allowed_mutates():
    """Backward compatible: mode=study still grades and mutates SM-2 fields."""
    card_doc = _make_card_doc()
    response, collection = await _post_review("&mode=study", card_doc)
    assert response.status_code == 200
    collection.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_mode_omitted_defaults_to_study_and_mutates():
    """Backward compatible: omitting mode defaults to study and still mutates."""
    card_doc = _make_card_doc()
    response, collection = await _post_review("", card_doc)
    assert response.status_code == 200
    collection.update_one.assert_awaited_once()
