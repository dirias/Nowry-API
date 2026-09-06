"""STUDY-001 — forecast, groups and narrowed daily-review sessions.

Pins the counting rules the Study Center's numbers depend on (docs/prd-study-center.md
US-006, docs/architecture.md addendum):

  1. The forecast starts TOMORROW in the user's local day; today is never in it.
  2. A card is struggling only inside the 14-day window, and `$first` after a
     descending sort is the latest grade.
  3. `limit` slices the selection already made, due cards first, and never
     widens the new-card pool.
  4. `group=marked` cannot narrow a study queue (ADR-014 point 2): validation
     refuses it, so the scheduler function never even names the mark.
  5. `group=struggling` on the list carries the grade back onto each card.

Setup follows test_marked_cards.py: a minimal app mounting only the router,
driven through httpx.ASGITransport; module-level collections are monkeypatched.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

_mock_agent_module = MagicMock()
_mock_agent_module.grant_xp = AsyncMock(return_value={})
sys.modules.setdefault("app.routers.agent", _mock_agent_module)

from app.routers import study_cards  # noqa: E402

OWNER = "507f1f77bcf86cd799439011"
DECK_A = ObjectId("507f1f77bcf86cd799439077")
DECK_B = ObjectId("507f1f77bcf86cd799439078")
CARD_1 = ObjectId("507f1f77bcf86cd799439101")
CARD_2 = ObjectId("507f1f77bcf86cd799439102")

_test_app = FastAPI()
_test_app.include_router(study_cards.router)


def _owner():
    return {"user_id": OWNER, "uid": "firebase-uid"}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sort = MagicMock(return_value=self)
        self.skip = MagicMock(return_value=self)
        self.limit = MagicMock(return_value=self)

    async def to_list(self, length=None):
        return list(self.rows)


class FakeCollection:
    """Answers `aggregate` from a callable on the pipeline and `find` with rows."""

    def __init__(self, aggregate=None, find_rows=None):
        self.pipelines = []
        self.queries = []
        self._aggregate = aggregate or (lambda p: [])
        self._find_rows = find_rows or []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return FakeCursor(self._aggregate(pipeline))

    def find(self, query, projection=None):
        self.queries.append(query)
        return FakeCursor(self._find_rows)

    async def count_documents(self, query):
        self.queries.append(query)
        return len(self._find_rows)


async def _get(client_path: str, cards: FakeCollection, monkeypatch, decks=None, sessions=None):
    monkeypatch.setattr(study_cards, "decks_collection", decks or FakeCollection(find_rows=[{"_id": DECK_A}, {"_id": DECK_B}]))
    monkeypatch.setattr(study_cards, "study_sessions_collection", sessions or FakeCollection())
    _test_app.dependency_overrides[study_cards.get_firebase_user] = _owner
    _test_app.dependency_overrides[study_cards.get_cards_collection] = lambda: cards
    try:
        transport = httpx.ASGITransport(app=_test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(client_path)
    finally:
        _test_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Forecast — starts tomorrow, in the user's day
# ---------------------------------------------------------------------------
def test_forecast_bounds_start_tomorrow_in_the_users_local_day():
    ny = ZoneInfo("America/New_York")
    bounds = study_cards._local_day_bounds_utc("America/New_York", 7)
    assert len(bounds) == 8  # 7 days need 8 edges
    tomorrow_local = (datetime.now(tz=ny) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert bounds[0] == tomorrow_local.astimezone(timezone.utc).replace(tzinfo=None)
    assert all((b - a) in (timedelta(hours=23), timedelta(hours=24), timedelta(hours=25)) for a, b in zip(bounds, bounds[1:]))
    with pytest.raises(Exception):
        study_cards._local_day_bounds_utc("Mars/Olympus", 7)


@pytest.mark.asyncio
async def test_forecast_buckets_next_review_by_day_and_never_counts_today(monkeypatch):
    bounds = study_cards._local_day_bounds_utc("UTC", 7)
    cards = FakeCollection(aggregate=lambda p: [{"_id": bounds[0], "due": 18}, {"_id": bounds[2], "due": 5}, {"_id": "other", "due": 99}])

    response = await _get("/study-cards/forecast?days=7&tz=UTC", cards, monkeypatch)

    assert response.status_code == 200
    body = response.json()
    assert [d["due"] for d in body["days"]] == [18, 0, 5, 0, 0, 0, 0]
    assert body["total"] == 23
    assert body["days"][0]["date"] == bounds[0].date().isoformat()
    match, bucket = cards.pipelines[0][0]["$match"], cards.pipelines[0][1]["$bucket"]
    assert match["next_review"] == {"$gte": bounds[0], "$lt": bounds[-1]}, "today and overdue are statistics' business"
    assert match["last_reviewed"] == {"$ne": None}, "a never-reviewed card has no real next_review"
    assert bucket["boundaries"] == bounds


# ---------------------------------------------------------------------------
# 2. Struggling — a 14-day window of again/hard grades, latest grade wins
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_struggling_is_a_fourteen_day_window_of_again_or_hard(monkeypatch):
    now = datetime(2026, 9, 6, 12, 0, 0)
    sessions = FakeCollection(aggregate=lambda p: [
        {"_id": str(CARD_1), "last_grade": "hard", "last_graded_at": now - timedelta(days=1)},
        {"_id": None, "last_grade": "again", "last_graded_at": now},
    ])
    monkeypatch.setattr(study_cards, "study_sessions_collection", sessions)

    result = await study_cards._struggling_cards(OWNER, now)

    assert set(result) == {str(CARD_1)}, "a row without a card id is dropped"
    assert result[str(CARD_1)]["last_grade"] == "hard"
    stages = sessions.pipelines[0]
    assert stages[0]["$match"]["completed_at"] == {"$gte": now - timedelta(days=14)}
    assert stages[1] == {"$sort": {"completed_at": -1}}, "$first must see the latest session first"
    assert stages[3]["$match"]["cards.grade"] == {"$in": ["again", "hard"]}
    assert stages[-1] == {"$limit": 2000}


# ---------------------------------------------------------------------------
# 3. Groups — tags and the two system groups share one counting rule
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_groups_returns_tags_sorted_and_the_two_system_groups(monkeypatch):
    def aggregate(pipeline):
        match = pipeline[0]["$match"]
        if any("$unwind" in stage for stage in pipeline):
            return [
                {"_id": "verbs", "cards": 46, "deck_ids": [DECK_A, str(DECK_A), DECK_B], "due": 9, "new": 2},
                {"_id": "asia", "cards": 140, "deck_ids": [DECK_B], "due": 0, "new": 0},
            ]
        if "marked_at" in match:
            return [{"_id": None, "cards": 6, "deck_ids": [DECK_A, DECK_B, None], "due": 2, "new": 1}]
        if "_id" in match:
            return [{"_id": None, "cards": 7, "deck_ids": [DECK_A], "due": 7, "new": 0}]
        return []

    cards = FakeCollection(aggregate=aggregate)
    sessions = FakeCollection(aggregate=lambda p: [{"_id": str(CARD_1), "last_grade": "again", "last_graded_at": datetime(2026, 9, 5)}])

    response = await _get("/study-cards/groups", cards, monkeypatch, sessions=sessions)

    assert response.status_code == 200
    body = response.json()
    assert body["tags"][0] == {"tag": "verbs", "cards": 46, "decks": 2, "due": 9, "new": 2}, "deck ids dedupe across ObjectId and string forms"
    assert body["system"][0] == {"key": "marked", "cards": 6, "decks": 2, "due": 2, "new": 1}
    assert body["system"][1] == {"key": "struggling", "cards": 7, "decks": 1, "due": 7, "new": 0, "window_days": 14}
    tag_pipeline = next(p for p in cards.pipelines if any("$unwind" in s for s in p))
    assert {"$sort": {"due": -1, "cards": -1, "_id": 1}} in tag_pipeline


@pytest.mark.asyncio
async def test_groups_struggling_is_empty_without_a_query_when_no_card_qualifies(monkeypatch):
    cards = FakeCollection(aggregate=lambda p: [])
    response = await _get("/study-cards/groups", cards, monkeypatch)
    assert response.json()["system"][1] == {"key": "struggling", "cards": 0, "decks": 0, "due": 0, "new": 0, "window_days": 14}
    assert not any("_id" in p[0]["$match"] for p in cards.pipelines), "no $in over an empty id list"


# ---------------------------------------------------------------------------
# 4 & 3. Daily review — narrowing and the cap
# ---------------------------------------------------------------------------
def _card(i, deck=DECK_A):
    return {"_id": ObjectId(f"507f1f77bcf86cd7994392{i:02d}"), "deck_id": deck, "user_id": OWNER, "title": f"c{i}"}


@pytest.mark.asyncio
async def test_daily_review_refuses_the_marked_group_before_the_scheduler_runs(monkeypatch):
    response = await _get("/study-cards/daily-review?group=marked", FakeCollection(), monkeypatch)
    assert response.status_code == 422, "refused by the query pattern, not by scheduler code"


@pytest.mark.asyncio
async def test_daily_review_limit_keeps_due_first_and_never_widens_the_selection(monkeypatch):
    calls = []

    async def fake_select(**kwargs):
        calls.append(kwargs)
        return [_card(1), _card(2), _card(3)], [_card(4), _card(5), _card(6), _card(7)]

    monkeypatch.setattr(study_cards, "_select_session_cards", fake_select)
    decks = FakeCollection(find_rows=[{"_id": DECK_A, "user_id": OWNER}])

    full = await _get("/study-cards/daily-review", FakeCollection(), monkeypatch, decks=decks)
    capped = await _get("/study-cards/daily-review?limit=5&tags=verbs", FakeCollection(), monkeypatch, decks=decks)

    assert full.json()["total"] == 7
    body = capped.json()
    assert body["total"] == 5
    assert [c["title"] for c in body["cards"]] == ["c1", "c4", "c5", "c6", "c7"], "all four due cards, then one new; session order unchanged"
    assert calls[0]["narrow"] is None
    assert calls[1]["narrow"] == {"tags": {"$in": ["verbs"]}}, "tags narrow the pool the budgets draw from"


@pytest.mark.asyncio
async def test_daily_review_struggling_narrows_by_card_id(monkeypatch):
    calls = []

    async def fake_select(**kwargs):
        calls.append(kwargs)
        return [], []

    monkeypatch.setattr(study_cards, "_select_session_cards", fake_select)
    sessions = FakeCollection(aggregate=lambda p: [{"_id": str(CARD_1), "last_grade": "again", "last_graded_at": datetime(2026, 9, 5)}])
    decks = FakeCollection(find_rows=[{"_id": DECK_A, "user_id": OWNER}])

    response = await _get("/study-cards/daily-review?group=struggling", FakeCollection(), monkeypatch, decks=decks, sessions=sessions)

    assert response.status_code == 200
    assert calls[0]["narrow"] == {"_id": {"$in": [CARD_1]}}


# ---------------------------------------------------------------------------
# 5. The list — group=struggling carries the grade back onto the card
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_group_struggling_annotates_cards_with_their_last_grade(monkeypatch):
    graded_at = datetime(2026, 9, 5, 9, 30)
    sessions = FakeCollection(aggregate=lambda p: [{"_id": str(CARD_1), "last_grade": "hard", "last_graded_at": graded_at}])
    cards = FakeCollection(find_rows=[{"_id": CARD_1, "deck_id": DECK_A, "user_id": OWNER, "title": "ser vs estar"}])

    response = await _get("/study-cards?group=struggling", cards, monkeypatch, sessions=sessions)

    assert response.status_code == 200
    card = response.json()["cards"][0]
    assert card["last_grade"] == "hard"
    assert card["last_graded_at"].startswith("2026-09-05T09:30")
    assert cards.queries[-1]["_id"] == {"$in": [CARD_1]}


@pytest.mark.asyncio
async def test_list_group_marked_is_the_marked_only_filter(monkeypatch):
    cards = FakeCollection(find_rows=[])
    response = await _get("/study-cards?group=marked", cards, monkeypatch)
    assert response.status_code == 200
    assert cards.queries[-1]["marked_at"] == {"$ne": None}
