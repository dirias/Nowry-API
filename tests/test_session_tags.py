"""
TAG-001 — the pool's tags on both session responses, and a deck session that
narrows by tags (ADR-044).

A study session's Tags list used to be the served queue's tags, so a tag whose
cards the day's budget had not picked was absent. Both session routes now carry
`tags: [{tag, new, due}]` over the UNNARROWED pool, and a deck session passes
`tags` into the selection the way the daily review has since STUDY-001.
"""
import pytest

from app.routers import study_cards
from tests.test_library_management import (
    DECK_T,
    OWNER,
    FakeCollection,
    _NoTagSettings,
    _cards_request,
    _deck_doc,
)


@pytest.fixture(autouse=True)
def _no_tag_settings(monkeypatch):
    monkeypatch.setattr(study_cards, "tag_settings_collection", _NoTagSettings())


POOL = [
    {"_id": "kanji-n3", "cards": 10, "deck_ids": [DECK_T], "due": 0, "new": 10, "resting": 0},
    {"_id": "verbs", "cards": 4, "deck_ids": [DECK_T], "due": 3, "new": 0, "resting": 1},
]


def _deck_session(monkeypatch, path: str, pool=POOL):
    cards = FakeCollection(aggregate=lambda pipeline: pool)
    decks = FakeCollection(find_rows=[_deck_doc()], find_one=lambda: _deck_doc())
    return cards, _cards_request("GET", path, monkeypatch, cards, decks_fake=decks)


@pytest.mark.asyncio
async def test_deck_session_carries_the_pools_tags(monkeypatch):
    cards, request = _deck_session(monkeypatch, f"/study-cards?deck_id={DECK_T}&due_only=true")
    response = await request
    assert response.status_code == 200
    body = response.json()
    assert body["tags"] == [
        {"tag": "kanji-n3", "new": 10, "due": 0},
        {"tag": "verbs", "new": 0, "due": 3},
    ]
    # The counting pipeline: the deck's cards, one row per tag, by tag. Every
    # tag of the pool, idle ones included (MOB-118, ADR-048).
    pipeline = cards.pipelines[-1]
    assert pipeline[0]["$match"]["$or"] == [{"deck_id": DECK_T}, {"deck_id": str(DECK_T)}]
    assert pipeline[0]["$match"]["user_id"] == OWNER
    assert {"$unwind": "$tags"} in pipeline
    assert pipeline[2]["$group"]["_id"] == "$tags"
    assert pipeline[3] == {"$sort": {"_id": 1}}
    assert not any("$expr" in stage.get("$match", {}) for stage in pipeline)


@pytest.mark.asyncio
async def test_deck_session_narrows_the_pool_by_tags(monkeypatch):
    cards, request = _deck_session(monkeypatch, f"/study-cards?deck_id={DECK_T}&due_only=true&tags=kanji-n3&tags=verbs")
    response = await request
    assert response.status_code == 200
    # Every selection query — the day's count, the sticky pool, the fresh
    # top-up, the review query — carries the tag narrowing.
    selection = [q for q in cards.queries if "last_reviewed" in q or "introduced_at" in q]
    assert selection, "the selection ran"
    for query in selection:
        assert query["tags"] == {"$in": ["kanji-n3", "verbs"]}
    # The pool's tags are still the whole deck's: the counting pipeline is not narrowed.
    assert "tags" not in cards.pipelines[-1][0]["$match"] or cards.pipelines[-1][0]["$match"]["tags"] == {"$exists": True, "$ne": []}
    assert response.json()["tags"][0]["tag"] == "kanji-n3"


@pytest.mark.asyncio
async def test_deck_session_without_tags_does_not_narrow(monkeypatch):
    cards, request = _deck_session(monkeypatch, f"/study-cards?deck_id={DECK_T}&due_only=true")
    await request
    selection = [q for q in cards.queries if "last_reviewed" in q or "introduced_at" in q]
    assert selection
    assert all("tags" not in q for q in selection)


@pytest.mark.asyncio
async def test_pool_rows_without_a_tag_name_are_dropped(monkeypatch):
    cards, request = _deck_session(monkeypatch, f"/study-cards?deck_id={DECK_T}&due_only=true", pool=[
        {"_id": None, "due": 1, "new": 0},
        {"_id": "  ", "due": 1, "new": 0},
        {"_id": "ok", "due": 1},
    ])
    body = (await request).json()
    assert body["tags"] == [{"tag": "ok", "new": 0, "due": 1}]


@pytest.mark.asyncio
async def test_daily_review_carries_the_pools_tags_over_the_active_decks(monkeypatch):
    cards = FakeCollection(aggregate=lambda pipeline: POOL)
    decks = FakeCollection(find_rows=[_deck_doc()])
    response = await _cards_request("GET", "/study-cards/daily-review", monkeypatch, cards, decks_fake=decks)
    assert response.status_code == 200
    body = response.json()
    assert body["tags"][0] == {"tag": "kanji-n3", "new": 10, "due": 0}
    match = cards.pipelines[-1][0]["$match"]
    assert match["deck_id"] == {"$in": [DECK_T, str(DECK_T)]}
    assert match["tags"] == {"$exists": True, "$ne": []}


@pytest.mark.asyncio
async def test_a_tag_with_nothing_waiting_is_listed_at_zero(monkeypatch):
    """MOB-118, ADR-048: an idle tag is named, so the phone's list matches the library's."""
    _, request = _deck_session(monkeypatch, f"/study-cards?deck_id={DECK_T}&due_only=true", pool=[
        {"_id": "grammar", "due": 0, "new": 0},
        {"_id": "verbs", "due": 3, "new": 0},
    ])
    body = (await request).json()
    assert body["tags"] == [
        {"tag": "grammar", "new": 0, "due": 0},
        {"tag": "verbs", "new": 0, "due": 3},
    ]
