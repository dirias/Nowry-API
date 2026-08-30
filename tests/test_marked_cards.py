"""MARK-001 — the user mark is an axis SM-2 cannot see, and vice versa (ADR-010).

These tests exist to make the independence structural rather than remembered.
Two directions are pinned:

  1. Marking a card writes `marked_at` and *nothing else* — no ease factor, no
     interval, no `next_review`. The mark route is deliberately narrow, and the
     narrowness is the guarantee, so a future edit that widens it fails here.
  2. The generic PATCH refuses scheduler state outright. Before this task it
     `$set` whatever it was handed, so any authenticated owner could reschedule
     a card by hand — `{"ease_factor": 2.5}` or `{"last_reviewed": ...}` — with
     no review ever taking place.

Setup follows test_study_cards_review_mode.py: a minimal FastAPI app mounting
only `study_cards.router`, driven through the real HTTP layer via
httpx.ASGITransport, so FastAPI's own Query-param resolution is genuinely
exercised. Importing the real `app.main` is avoided for the pre-existing
Python 3.9 reasons documented at length in that file.
"""
from __future__ import annotations

import sys
import pytest
import httpx
from fastapi import FastAPI
from unittest.mock import AsyncMock, MagicMock
from bson import ObjectId

# Same lazy-import stub as test_study_cards_review_mode.py: review_card pulls
# app.routers.agent for its XP side-effect, which drags in SDKs absent from this
# dev venv. Nothing here exercises XP, but the router import must succeed.
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

CARD_ID = "507f1f77bcf86cd799439099"
DECK_ID = "507f1f77bcf86cd799439077"
OWNER_USER_ID = "507f1f77bcf86cd799439011"  # matches conftest.mock_firebase_user
OTHER_USER_ID = "507f1f77bcf86cd799439022"

#: Everything the scheduler owns. Marking must leave every one of these alone,
#: and the generic PATCH must refuse every one of them.
SM2_FIELDS = (
    "ease_factor",
    "interval",
    "repetitions",
    "next_review",
    "last_reviewed",
    "introduced_at",
)

_test_app = FastAPI()
_test_app.include_router(study_cards.router)


async def _mock_owner_user():
    return {
        "user_id": OWNER_USER_ID,
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
    }


def _make_card_doc(**overrides) -> dict:
    """A fresh, mid-schedule card. Fresh per call because require_ownership
    rewrites `_id` in place."""
    doc = {
        "_id": ObjectId(CARD_ID),
        "user_id": OWNER_USER_ID,
        "deck_id": ObjectId(DECK_ID),
        "title": "Mitochondrion",
        "content": "The powerhouse of the cell.",
        "deleted_at": None,
        "ease_factor": 2.36,
        "interval": 6,
        "repetitions": 3,
        "next_review": None,
        "last_reviewed": None,
        "introduced_at": None,
        "marked_at": None,
    }
    doc.update(overrides)
    return doc


def _make_collection(card_doc: dict) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=card_doc)
    collection.update_one = AsyncMock(return_value=None)
    return collection


async def _request(method: str, path: str, card_doc: dict, json=None):
    """Drive one request against the minimal app with the owner identity."""
    collection = _make_collection(card_doc)
    _test_app.dependency_overrides[study_cards.get_firebase_user] = _mock_owner_user
    _test_app.dependency_overrides[study_cards.get_cards_collection] = lambda: collection
    _test_app.dependency_overrides[study_cards.get_decks_collection] = lambda: MagicMock()

    try:
        transport = httpx.ASGITransport(app=_test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.request(method, path, json=json)
    finally:
        _test_app.dependency_overrides.clear()

    return response, collection


def _written_fields(collection: MagicMock) -> set:
    """The exact set of keys the route `$set`."""
    collection.update_one.assert_awaited_once()
    _filter, update = collection.update_one.await_args[0]
    return set(update["$set"].keys())


# ---------------------------------------------------------------------------
# The mark route writes one field, and it is not a scheduler field
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_marking_writes_marked_at_and_nothing_from_sm2():
    """The core ADR-010 guarantee, asserted on the write itself."""
    response, collection = await _request("PUT", f"/study-cards/{CARD_ID}/mark", _make_card_doc())

    assert response.status_code == 200
    written = _written_fields(collection)
    assert written == {"marked_at", "updated_at"}
    for field in SM2_FIELDS:
        assert field not in written


@pytest.mark.asyncio
async def test_marking_sets_a_timestamp_not_a_boolean():
    """`marked_at` orders the deferred cross-deck session (MARK-007), so the
    stored value has to be a real datetime rather than a truthy flag."""
    from datetime import datetime

    _response, collection = await _request(
        "PUT", f"/study-cards/{CARD_ID}/mark", _make_card_doc()
    )

    _filter, update = collection.update_one.await_args[0]
    assert isinstance(update["$set"]["marked_at"], datetime)


@pytest.mark.asyncio
async def test_unmarking_clears_the_mark_and_touches_nothing_else():
    response, collection = await _request(
        "DELETE", f"/study-cards/{CARD_ID}/mark", _make_card_doc(marked_at="2026-08-30T10:00:00")
    )

    assert response.status_code == 200
    _filter, update = collection.update_one.await_args[0]
    assert update["$set"]["marked_at"] is None
    assert set(update["$set"].keys()) == {"marked_at", "updated_at"}


@pytest.mark.asyncio
async def test_marking_an_already_marked_card_succeeds():
    """A double-tap must be harmless, not a 409."""
    response, _collection = await _request(
        "PUT", f"/study-cards/{CARD_ID}/mark", _make_card_doc(marked_at="2026-08-30T10:00:00")
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_unmarking_an_unmarked_card_succeeds():
    response, _collection = await _request(
        "DELETE", f"/study-cards/{CARD_ID}/mark", _make_card_doc()
    )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_mark_routes_refuse_another_users_card(method):
    """Ownership is enforced by the same dependency as every other card route,
    which answers 403 rather than 404 for a card that exists but is not yours."""
    foreign = _make_card_doc(user_id=OTHER_USER_ID)
    response, collection = await _request(method, f"/study-cards/{CARD_ID}/mark", foreign)

    assert response.status_code == 403
    collection.update_one.assert_not_awaited()


# ---------------------------------------------------------------------------
# The generic PATCH no longer accepts scheduler state
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value",
    [
        ("ease_factor", 2.5),
        ("interval", 365),
        ("repetitions", 99),
        ("next_review", "2030-01-01T00:00:00"),
        ("last_reviewed", "2030-01-01T00:00:00"),
        ("introduced_at", "2030-01-01T00:00:00"),
        ("marked_at", "2030-01-01T00:00:00"),
    ],
)
async def test_patch_refuses_protected_fields(field, value):
    """Each of these was silently applied before MARK-001.

    `next_review` is the obvious one, but `last_reviewed` is why the set is not
    just the four SM-2 numbers: setting it used to recompute `next_review`, so
    blocking the front door while leaving that open would have changed nothing.
    `introduced_at` drives which new cards a day's session locks in, and
    `marked_at` belongs to the mark route alone.
    """
    response, collection = await _request(
        "PATCH", f"/study-cards/{CARD_ID}", _make_card_doc(), json={field: value}
    )

    assert response.status_code == 400
    assert field in response.json()["detail"]
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_names_every_offending_field_at_once():
    response, _collection = await _request(
        "PATCH",
        f"/study-cards/{CARD_ID}",
        _make_card_doc(),
        json={"ease_factor": 2.5, "repetitions": 0, "title": "Still rejected"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "ease_factor" in detail and "repetitions" in detail


@pytest.mark.asyncio
async def test_patch_still_edits_ordinary_card_content():
    """The hardening must restrict, not over-restrict: the one real caller
    (useCardForm) edits title/content/tags and has to keep working."""
    response, collection = await _request(
        "PATCH",
        f"/study-cards/{CARD_ID}",
        _make_card_doc(),
        json={"title": "Mitochondria", "content": "Site of ATP synthesis.", "tags": ["biology"]},
    )

    assert response.status_code == 200
    assert _written_fields(collection) == {"title", "content", "tags"}


# ---------------------------------------------------------------------------
# `marked_only` narrows the list without disturbing the other filters
# ---------------------------------------------------------------------------
class FakeCardsCollection:
    """Records the filter the list endpoint builds, and answers from it."""

    def __init__(self, docs: list) -> None:
        self.docs = docs
        self.last_filter: dict = {}

    def _selected(self) -> list:
        return [doc for doc in self.docs if _matches(doc, self.last_filter)]

    def find(self, query: dict):
        self.last_filter = query
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.skip = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=list(self._selected()))
        return cursor

    async def count_documents(self, query: dict) -> int:
        self.last_filter = query
        return len(self._selected())


_MISSING = object()


def _matches_operator(value, operator: str, expected) -> bool:
    if operator == "$ne":
        return (None if value is _MISSING else value) != expected
    if operator == "$in":
        candidates = value if isinstance(value, list) else [value]
        return any(item in expected for item in candidates)
    if operator == "$exists":
        return (value is not _MISSING) is bool(expected)
    raise AssertionError(f"unsupported operator in card list query: {operator}")


def _matches(doc: dict, query: dict) -> bool:
    for field, expected in query.items():
        if field == "$and":
            if not all(_matches(doc, clause) for clause in expected):
                return False
        elif field == "$or":
            if not any(_matches(doc, clause) for clause in expected):
                return False
        else:
            value = doc.get(field, _MISSING)
            if isinstance(expected, dict):
                if not all(
                    _matches_operator(value, operator, operand)
                    for operator, operand in expected.items()
                ):
                    return False
            elif (None if value is _MISSING else value) != expected:
                return False
    return True


MARKED_CARD = _make_card_doc(marked_at="2026-08-30T10:00:00")
UNMARKED_CARD = _make_card_doc(marked_at=None)
UNMARKED_CARD["_id"] = ObjectId("507f1f77bcf86cd799439088")
LEGACY_CARD = _make_card_doc()  # predates the field entirely
LEGACY_CARD["_id"] = ObjectId("507f1f77bcf86cd799439066")
del LEGACY_CARD["marked_at"]


async def _list_cards(query_string: str):
    collection = FakeCardsCollection([MARKED_CARD, UNMARKED_CARD, LEGACY_CARD])
    _test_app.dependency_overrides[study_cards.get_firebase_user] = _mock_owner_user
    _test_app.dependency_overrides[study_cards.get_cards_collection] = lambda: collection

    try:
        transport = httpx.ASGITransport(app=_test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/study-cards?deck_id={DECK_ID}&{query_string}")
    finally:
        _test_app.dependency_overrides.clear()

    return response, collection


@pytest.mark.asyncio
async def test_marked_only_returns_just_the_marked_card():
    response, _collection = await _list_cards("marked_only=true")

    assert response.status_code == 200
    returned = {card["_id"] for card in response.json()["cards"]}
    assert returned == {str(MARKED_CARD["_id"])}


@pytest.mark.asyncio
async def test_marked_only_excludes_cards_that_predate_the_field():
    """Mongo treats a missing key as null, so `$ne: None` must not match it."""
    response, _collection = await _list_cards("marked_only=true")
    returned = {card["_id"] for card in response.json()["cards"]}
    assert str(LEGACY_CARD["_id"]) not in returned


@pytest.mark.asyncio
async def test_marked_only_defaults_off_and_leaves_the_query_untouched():
    """Every existing caller must build exactly the query it built before."""
    response, collection = await _list_cards("")

    assert response.status_code == 200
    assert "marked_at" not in collection.last_filter
    assert len(response.json()["cards"]) == 3


@pytest.mark.asyncio
async def test_marked_filter_survives_the_deck_clause():
    """The deck filter claims the top-level `$or`; the mark must not compete
    for that key or be dissolved by it."""
    _response, collection = await _list_cards("marked_only=true")

    assert collection.last_filter["marked_at"] == {"$ne": None}
    assert "$or" in collection.last_filter
