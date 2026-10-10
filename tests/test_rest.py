"""SR-001 — rest is membership, materialized on the card; the scheduler keeps the schedule (ADR-043).

Pinned here, the way test_marked_cards.py pins the mark:

  1. A rest or wake writes exactly the rest fields — `rests`, `rest_until`,
     `rested_at`, `updated_at` — and nothing the scheduler owns.
  2. The generic PATCH refuses the rest fields outright.
  3. The derived field and the awake clause agree: a dated rest ends by itself.
  4. Every place "due" is computed carries the awake clause, read from source.
  5. The bulk verbs: `until` belongs to `rest` only.

Setup follows test_marked_cards.py: a minimal FastAPI app mounting only the
router under test, driven through httpx.ASGITransport.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

_mock_agent_module = MagicMock()
_mock_agent_module.grant_xp = AsyncMock(return_value={"level_up": False, "new_level": 1, "new_stage": 1, "avatar_regen_pending": False})
sys.modules.setdefault("app.routers.agent", _mock_agent_module)

from app.routers import study_cards  # noqa: E402
from app.services import rest  # noqa: E402

CARD_ID = "507f1f77bcf86cd799439099"
DECK_ID = "507f1f77bcf86cd799439077"
OWNER_USER_ID = "507f1f77bcf86cd799439011"

SM2_FIELDS = ("ease_factor", "interval", "repetitions", "next_review", "last_reviewed", "introduced_at")

_test_app = FastAPI()
_test_app.include_router(study_cards.router)


async def _mock_owner_user():
    return {"user_id": OWNER_USER_ID, "firebase_uid": "test-firebase-uid-123", "email": "test@example.com"}


def _make_card_doc(**overrides) -> dict:
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
        "next_review": datetime(2026, 10, 19),
        "last_reviewed": datetime(2026, 10, 13),
        "marked_at": None,
        "rest_until": None,
        "rests": [],
        "tags": ["verbs"],
    }
    doc.update(overrides)
    return doc


class _FakeUpdateResult:
    matched_count = 1


@pytest.fixture
def cards_collection():
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=_make_card_doc())
    collection.update_many = AsyncMock(return_value=_FakeUpdateResult())
    collection.update_one = AsyncMock(return_value=_FakeUpdateResult())
    # The bulk route narrows ids to the caller's own cards through `find(...).to_list`.
    collection.find = MagicMock()
    collection.find.return_value.to_list = AsyncMock(return_value=[{"_id": ObjectId(CARD_ID), "deck_id": ObjectId(DECK_ID)}])
    return collection


@pytest.fixture
def client(cards_collection):
    _test_app.dependency_overrides[study_cards.get_cards_collection] = lambda: cards_collection
    _test_app.dependency_overrides[study_cards.get_firebase_user] = _mock_owner_user
    transport = httpx.ASGITransport(app=_test_app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")
    _test_app.dependency_overrides.clear()


# --- 1. The writer's narrowness -------------------------------------------------

def _set_keys(pipeline: list) -> set:
    keys: set = set()
    for stage in pipeline:
        keys |= set(stage["$set"].keys())
    return keys


def test_rest_update_writes_only_rest_fields():
    now = datetime(2026, 10, 10, 12, 0)
    keys = _set_keys(rest.rest_update(rest.SOURCE_CARD, rest.INDEFINITE, now))
    assert keys == set(rest.REST_FIELDS)
    assert not keys & set(SM2_FIELDS)


def test_wake_update_writes_only_rest_fields():
    now = datetime(2026, 10, 10, 12, 0)
    keys = _set_keys(rest.wake_update(now, source=rest.SOURCE_CARD))
    assert keys <= set(rest.REST_FIELDS)
    assert "rested_at" not in keys, "waking never forgets the first rest"
    assert not keys & set(SM2_FIELDS)


@pytest.mark.asyncio
async def test_rest_route_writes_through_the_pipeline_and_nothing_else(client, cards_collection):
    async with client as c:
        response = await c.put(f"/study-cards/{CARD_ID}/rest", json={})
    assert response.status_code == 200, response.text
    match, pipeline = cards_collection.update_many.call_args.args
    assert match == {"_id": ObjectId(CARD_ID)}
    assert _set_keys(pipeline) == set(rest.REST_FIELDS)
    # The appended entry is the card's own hold, until woken.
    entry = pipeline[0]["$set"]["rests"]["$concatArrays"][1][0]
    assert entry["source"] == rest.SOURCE_CARD
    assert entry["until"] == rest.INDEFINITE


@pytest.mark.asyncio
async def test_rest_until_a_date_is_carried(client, cards_collection):
    async with client as c:
        response = await c.put(f"/study-cards/{CARD_ID}/rest", json={"until": "2026-11-02T00:00:00Z"})
    assert response.status_code == 200, response.text
    _, pipeline = cards_collection.update_many.call_args.args
    entry = pipeline[0]["$set"]["rests"]["$concatArrays"][1][0]
    assert entry["until"] == datetime(2026, 11, 2)


@pytest.mark.asyncio
async def test_wake_route_drops_only_the_cards_own_source(client, cards_collection):
    async with client as c:
        response = await c.delete(f"/study-cards/{CARD_ID}/rest")
    assert response.status_code == 200, response.text
    _, pipeline = cards_collection.update_many.call_args.args
    conditions = pipeline[0]["$set"]["rests"]["$filter"]["cond"]["$and"]
    assert {"$ne": ["$$this.source", rest.SOURCE_CARD]} in conditions


@pytest.mark.asyncio
async def test_rest_body_refuses_unknown_keys(client):
    async with client as c:
        response = await c.put(f"/study-cards/{CARD_ID}/rest", json={"ease_factor": 1.3})
    assert response.status_code == 422


# --- 2. The generic PATCH refuses the fields -------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["rest_until", "rests", "rested_at"])
async def test_patch_refuses_rest_fields(client, cards_collection, field):
    async with client as c:
        response = await c.patch(f"/study-cards/{CARD_ID}", json={field: None})
    assert response.status_code == 422, response.text
    cards_collection.update_one.assert_not_called()


# --- 3. The derived field and the clause agree ----------------------------------

def test_derive_rest_until_is_the_latest_live_source():
    now = datetime(2026, 10, 10)
    rests = [
        {"source": "card", "until": datetime(2026, 10, 12)},
        {"source": "tag:kanji", "until": datetime(2026, 11, 2)},
        {"source": "deck", "until": datetime(2026, 10, 1)},  # spent
    ]
    assert rest.derive_rest_until(rests, now) == datetime(2026, 11, 2)


def test_derive_rest_until_is_none_when_every_source_is_spent_or_absent():
    now = datetime(2026, 10, 10)
    assert rest.derive_rest_until([], now) is None
    assert rest.derive_rest_until(None, now) is None
    assert rest.derive_rest_until([{"source": "card", "until": now}], now) is None, "at `now` the rest has ended"


def test_indefinite_outlasts_any_date():
    now = datetime(2026, 10, 10)
    assert rest.derive_rest_until([{"source": "card", "until": rest.INDEFINITE}], now) == rest.INDEFINITE
    assert rest.until_or_indefinite(None) == rest.INDEFINITE
    assert rest.until_or_indefinite(datetime(2026, 11, 2)) == datetime(2026, 11, 2)


def test_awake_clause_matches_missing_null_and_past_but_not_future():
    now = datetime(2026, 10, 10)
    clause = rest.awake_clause(now)
    assert clause == {"rest_until": {"$not": {"$gt": now}}}
    assert rest.resting_clause(now) == {"rest_until": {"$gt": now}}
    # One key, so it composes into a match that already carries an `$or`.
    assert set(clause) == {"rest_until"}


# --- 4. Every due computation carries the clause --------------------------------

def test_every_due_site_carries_the_awake_clause():
    root = Path(__file__).resolve().parents[1] / "app" / "routers"
    cards_src = (root / "study_cards.py").read_text()
    decks_src = (root / "decks.py").read_text()
    # The selection (base match + fresh top-up), the groups stage, the
    # forecast, due_only, and the deck stats (due, new, next card).
    assert cards_src.count("awake_clause(") >= 4, "selection ×2, forecast, due_only"
    assert "resting_expr(now_dt)" in cards_src, "the groups stage"
    assert decks_src.count("awake_clause(now_dt)") >= 3, "deck due, new, next card"
    assert "resting_clause(now_dt)" in decks_src, "deck resting count"
    # And the mark's rule still holds: nothing here writes a scheduler field.
    for stage in re.findall(r"rest_update\(|wake_update\(", cards_src):
        assert stage


# --- 5. Bulk ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bulk_until_belongs_to_rest_only(client):
    async with client as c:
        bad = await c.post("/study-cards/bulk", json={"ids": [CARD_ID], "action": "wake", "until": "2026-11-02T00:00:00Z"})
        good = await c.post("/study-cards/bulk", json={"ids": [CARD_ID], "action": "rest", "until": "2026-11-02T00:00:00Z"})
    assert bad.status_code == 422
    assert good.status_code == 200, good.text


@pytest.mark.asyncio
async def test_bulk_rest_writes_the_cards_own_hold(client, cards_collection):
    async with client as c:
        response = await c.post("/study-cards/bulk", json={"ids": [CARD_ID], "action": "rest"})
    assert response.status_code == 200, response.text
    match, pipeline = cards_collection.update_many.call_args.args
    assert match == {"_id": {"$in": [ObjectId(CARD_ID)]}}
    assert _set_keys(pipeline) == set(rest.REST_FIELDS)
