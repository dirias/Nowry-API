"""MGMT-001 — untagged, the bulk writer, the tag verbs and archive (ADR-023).

Pins the rules the library's management verbs depend on (docs/prd-study-center.md
D15–D18, FR-009–FR-012; docs/architecture.md "Library management" addendum):

  1. `untagged=true` is the no-tag `$or`; with `tags` it is a UNION, folded into
     `$and` beside the deck `$or` so nothing collides.
  2. `/groups` carries `untagged: {cards, due, new}` under the same scope.
  3. A bulk `move` decrements every old deck by its own count, increments the
     target once per card, and ignores a foreign id.
  4. A rename onto an existing tag pulls `from` where `to` is already there and
     rewrites the element everywhere else; `remove` reports its count.
  5. `mark` / `unmark` in bulk go through `_write_mark_many` (ADR-010's one writer).
  6. `_active_deck_or` reads `archived_at: None`; statistics' counts take it.
  7. Archive sets `archived_at` + `status: archived`; restore clears the stamp
     and derives the status; `GET /decks` excludes archived by default.

Setup follows test_study_center_groups.py: a minimal app mounting only the
router, driven through httpx.ASGITransport; module-level collections are
monkeypatched with fakes that record every query and write.
"""
from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

_mock_agent_module = MagicMock()
_mock_agent_module.grant_xp = AsyncMock(return_value={})
sys.modules.setdefault("app.routers.agent", _mock_agent_module)

from app.routers import decks, study_cards  # noqa: E402

OWNER = "507f1f77bcf86cd799439011"
OTHER = "507f1f77bcf86cd799439022"
DECK_A = ObjectId("507f1f77bcf86cd799439077")
DECK_B = ObjectId("507f1f77bcf86cd799439078")
DECK_T = ObjectId("507f1f77bcf86cd799439079")
CARD_1 = ObjectId("507f1f77bcf86cd799439101")
CARD_2 = ObjectId("507f1f77bcf86cd799439102")
CARD_3 = ObjectId("507f1f77bcf86cd799439103")
FOREIGN = ObjectId("507f1f77bcf86cd799439999")

UNTAGGED_OR = [{"tags": None}, {"tags": {"$exists": False}}, {"tags": []}]

_cards_app = FastAPI()
_cards_app.include_router(study_cards.router)
_decks_app = FastAPI()
_decks_app.include_router(decks.router)


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


class FakeResult:
    def __init__(self, matched: int):
        self.matched_count = matched


class FakeCollection:
    """Records every query, pipeline and write; answers from fixed rows."""

    def __init__(
        self,
        find_rows: Optional[list] = None,
        aggregate: Optional[Callable] = None,
        find_one: Optional[Callable[[], Optional[dict]]] = None,
        matched: int = 0,
    ):
        self.queries: list = []
        self.pipelines: list = []
        self.writes: list = []  # (kind, query, update, kwargs)
        self._find_rows = find_rows or []
        self._aggregate = aggregate or (lambda p: [])
        self._find_one = find_one or (lambda: None)
        self._matched = matched

    def find(self, query, projection=None):
        self.queries.append(query)
        return FakeCursor(self._find_rows)

    async def count_documents(self, query):
        self.queries.append(query)
        return len(self._find_rows)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return FakeCursor(self._aggregate(pipeline))

    async def find_one(self, query, **kwargs):
        self.queries.append(query)
        return self._find_one()

    async def update_one(self, query, update, **kwargs):
        self.writes.append(("one", query, update, kwargs))
        return FakeResult(1)

    async def update_many(self, query, update, **kwargs):
        self.writes.append(("many", query, update, kwargs))
        return FakeResult(self._matched)


def _deck_doc(oid: ObjectId = DECK_T, user_id: str = OWNER, **extra) -> dict:
    return {"_id": oid, "user_id": user_id, "name": "d", "deleted_at": None, "cards": [], **extra}


async def _cards_request(
    method: str, path: str, monkeypatch, cards: FakeCollection, json: Any = None,
    decks_fake: Optional[FakeCollection] = None,
):
    decks_fake = decks_fake or FakeCollection(find_rows=[{"_id": DECK_A}, {"_id": DECK_B}])
    monkeypatch.setattr(study_cards, "decks_collection", decks_fake)
    monkeypatch.setattr(study_cards, "study_sessions_collection", FakeCollection())
    monkeypatch.setattr(study_cards, "books_collection", FakeCollection())
    _cards_app.dependency_overrides[study_cards.get_firebase_user] = _owner
    _cards_app.dependency_overrides[study_cards.get_cards_collection] = lambda: cards
    _cards_app.dependency_overrides[study_cards.get_decks_collection] = lambda: decks_fake
    try:
        transport = httpx.ASGITransport(app=_cards_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json)
    finally:
        _cards_app.dependency_overrides.clear()


async def _decks_request(method: str, path: str, monkeypatch, decks_fake: FakeCollection, cards: FakeCollection):
    monkeypatch.setattr(decks, "cards_collection", cards)
    _decks_app.dependency_overrides[decks.get_firebase_user] = _owner
    _decks_app.dependency_overrides[decks.get_decks_collection] = lambda: decks_fake
    try:
        transport = httpx.ASGITransport(app=_decks_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path)
    finally:
        _decks_app.dependency_overrides.clear()


def _and_clauses(query: dict) -> list:
    return query.get("$and", [])


# ---------------------------------------------------------------------------
# 1. untagged — the filter and its union with tags
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_untagged_alone_is_the_no_tag_or_beside_the_deck_scope(monkeypatch):
    cards = FakeCollection(find_rows=[])
    response = await _cards_request("GET", "/study-cards?untagged=true", monkeypatch, cards)

    assert response.status_code == 200
    query = cards.queries[-1]
    assert "$or" not in query, "the deck $or moved into $and so the two never collide"
    deck_or, tag_or = _and_clauses(query)
    assert deck_or["$or"][0] == {"deck_id": None}
    assert tag_or == {"$or": UNTAGGED_OR}
    assert "tags" not in query


@pytest.mark.asyncio
async def test_untagged_with_tags_is_a_union(monkeypatch):
    cards = FakeCollection(find_rows=[])
    response = await _cards_request("GET", "/study-cards?untagged=true&tags=verbs&tags=asia", monkeypatch, cards)

    assert response.status_code == 200
    query = cards.queries[-1]
    _, tag_or = _and_clauses(query)
    assert tag_or == {"$or": [{"tags": {"$in": ["verbs", "asia"]}}, *UNTAGGED_OR]}
    assert "tags" not in query, "the union replaces the plain tags key, it does not sit beside it"


@pytest.mark.asyncio
async def test_tags_alone_keeps_the_plain_key_and_search_still_composes(monkeypatch):
    cards = FakeCollection(find_rows=[])
    await _cards_request("GET", "/study-cards?tags=verbs", monkeypatch, cards)
    assert cards.queries[-1]["tags"] == {"$in": ["verbs"]}
    assert "$and" not in cards.queries[-1]

    await _cards_request("GET", "/study-cards?untagged=true&search=ser&due_only=true", monkeypatch, cards)
    clauses = _and_clauses(cards.queries[-1])
    assert [list(c.keys()) for c in clauses] == [["$or"]] * 4, "deck, untagged, search, due — each its own $or"
    assert clauses[1] == {"$or": UNTAGGED_OR}
    assert "title" in clauses[2]["$or"][0]
    assert "next_review" in clauses[3]["$or"][0]


# ---------------------------------------------------------------------------
# 2. groups — untagged is a readout under the same scope
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_groups_carries_an_untagged_readout_with_the_same_counting_rule(monkeypatch):
    def aggregate(pipeline):
        match = pipeline[0]["$match"]
        if "$and" in match:
            return [{"_id": None, "cards": 85, "deck_ids": [DECK_A], "due": 6, "new": 12}]
        return []

    cards = FakeCollection(aggregate=aggregate)
    response = await _cards_request("GET", "/study-cards/groups", monkeypatch, cards)

    assert response.status_code == 200
    body = response.json()
    assert body["untagged"] == {"cards": 85, "due": 6, "new": 12}, "cards, due, new — no deck list, it is not a group"
    assert [g["key"] for g in body["system"]] == ["marked", "struggling"], "untagged is never a third system group"
    untagged_pipeline = next(p for p in cards.pipelines if "$and" in p[0]["$match"])
    match = untagged_pipeline[0]["$match"]
    assert match["user_id"] == OWNER and match["deleted_at"] is None
    assert "$or" not in match
    assert match["$and"][1] == {"$or": UNTAGGED_OR}
    assert match["$and"][0]["$or"][0] == {"deck_id": None}
    assert "$group" in untagged_pipeline[1] and "due" in untagged_pipeline[1]["$group"]


# ---------------------------------------------------------------------------
# 3. bulk move — counters per old deck, target once per card, foreign id ignored
# ---------------------------------------------------------------------------
def _owned_docs() -> list:
    return [
        {"_id": CARD_1, "deck_id": DECK_A},
        {"_id": CARD_2, "deck_id": DECK_A},
        {"_id": CARD_3, "deck_id": str(DECK_B)},
    ]


@pytest.mark.asyncio
async def test_bulk_move_keeps_every_deck_counter_and_ignores_a_foreign_id(monkeypatch):
    cards = FakeCollection(find_rows=_owned_docs())
    decks_fake = FakeCollection(find_one=lambda: _deck_doc(DECK_T))
    body = {"ids": [str(CARD_1), str(CARD_2), str(CARD_3), str(FOREIGN)], "action": "move", "deck_id": str(DECK_T)}

    response = await _cards_request("POST", "/study-cards/bulk", monkeypatch, cards, json=body, decks_fake=decks_fake)

    assert response.status_code == 200
    assert response.json() == {"updated": 3}, "the foreign id is neither touched nor counted"
    lookup = cards.queries[0]
    assert lookup["user_id"] == OWNER and lookup["deleted_at"] is None
    assert set(lookup["_id"]["$in"]) == {CARD_1, CARD_2, CARD_3, FOREIGN}

    deck_writes = {str(q["_id"]): u for kind, q, u, _ in decks_fake.writes}
    assert deck_writes[str(DECK_A)] == {"$inc": {"total_cards": -2}, "$pull": {"cards": {"$in": [CARD_1, CARD_2]}}}
    assert deck_writes[str(DECK_B)] == {"$inc": {"total_cards": -1}, "$pull": {"cards": {"$in": [CARD_3]}}}
    assert deck_writes[str(DECK_T)] == {"$inc": {"total_cards": 3}, "$push": {"cards": {"$each": [CARD_1, CARD_2, CARD_3]}}}

    kind, query, update, _ = cards.writes[-1]
    assert kind == "many" and query == {"_id": {"$in": [CARD_1, CARD_2, CARD_3]}}
    assert update["$set"]["deck_id"] == DECK_T and "updated_at" in update["$set"]


@pytest.mark.asyncio
async def test_bulk_move_to_null_removes_from_the_deck_and_refuses_a_foreign_deck(monkeypatch):
    cards = FakeCollection(find_rows=[{"_id": CARD_1, "deck_id": DECK_A}])
    decks_fake = FakeCollection(find_one=lambda: _deck_doc(DECK_T))
    response = await _cards_request(
        "POST", "/study-cards/bulk", monkeypatch, cards, json={"ids": [str(CARD_1)], "action": "move", "deck_id": None}, decks_fake=decks_fake,
    )
    assert response.status_code == 200 and response.json() == {"updated": 1}
    assert [str(q["_id"]) for _, q, _, _ in decks_fake.writes] == [str(DECK_A)], "no target to push onto"
    assert cards.writes[-1][2]["$set"]["deck_id"] is None

    foreign_decks = FakeCollection(find_one=lambda: _deck_doc(DECK_T, user_id=OTHER))
    response = await _cards_request(
        "POST", "/study-cards/bulk", monkeypatch, FakeCollection(find_rows=[]), json={"ids": [str(CARD_1)], "action": "move", "deck_id": str(DECK_T)}, decks_fake=foreign_decks,
    )
    assert response.status_code == 403
    assert foreign_decks.writes == []


@pytest.mark.asyncio
async def test_bulk_refuses_a_verb_without_its_field_or_an_unknown_verb(monkeypatch):
    for body in (
        {"ids": [str(CARD_1)], "action": "move"},
        {"ids": [str(CARD_1)], "action": "tag"},
        {"ids": [str(CARD_1)], "action": "untag", "tags": []},
        {"ids": [str(CARD_1)], "action": "suspend"},
        {"ids": [], "action": "mark"},
        {"ids": [str(CARD_1)], "action": "mark", "ease_factor": 2.5},
    ):
        cards = FakeCollection(find_rows=[{"_id": CARD_1, "deck_id": None}])
        response = await _cards_request("POST", "/study-cards/bulk", monkeypatch, cards, json=body)
        assert response.status_code == 422, body
        assert cards.writes == []


@pytest.mark.asyncio
async def test_bulk_delete_soft_deletes_and_decrements_each_deck(monkeypatch):
    cards = FakeCollection(find_rows=_owned_docs())
    decks_fake = FakeCollection()
    response = await _cards_request(
        "POST", "/study-cards/bulk", monkeypatch, cards, json={"ids": [str(CARD_1), str(CARD_2), str(CARD_3)], "action": "delete"}, decks_fake=decks_fake,
    )
    assert response.status_code == 200 and response.json() == {"updated": 3}
    assert {str(q["_id"]): u["$inc"]["total_cards"] for _, q, u, _ in decks_fake.writes} == {str(DECK_A): -2, str(DECK_B): -1}
    kind, query, update, _ = cards.writes[-1]
    assert kind == "many" and query == {"_id": {"$in": [CARD_1, CARD_2, CARD_3]}}
    assert set(update["$set"]) == {"deleted_at", "deleted_by", "updated_at"}
    assert update["$set"]["deleted_by"] == OWNER


@pytest.mark.asyncio
async def test_bulk_tag_adds_to_set_and_untag_pulls_with_in(monkeypatch):
    cards = FakeCollection(find_rows=[{"_id": CARD_1, "deck_id": None}, {"_id": CARD_2, "deck_id": None}], matched=2)
    response = await _cards_request(
        "POST", "/study-cards/bulk", monkeypatch, cards, json={"ids": [str(CARD_1), str(CARD_2)], "action": "tag", "tags": [" verbs ", "verbs", "asia"]},
    )
    assert response.status_code == 200 and response.json() == {"updated": 2}
    null_fix, add = cards.writes
    assert null_fix[1] == {"_id": {"$in": [CARD_1, CARD_2]}, "tags": None} and null_fix[2] == {"$set": {"tags": []}}
    assert add[2]["$addToSet"] == {"tags": {"$each": ["verbs", "asia"]}}, "trimmed and deduped like a card's own tags"
    assert "updated_at" in add[2]["$set"]

    cards = FakeCollection(find_rows=[{"_id": CARD_1, "deck_id": None}], matched=1)
    response = await _cards_request(
        "POST", "/study-cards/bulk", monkeypatch, cards, json={"ids": [str(CARD_1)], "action": "untag", "tags": ["verbs"]},
    )
    assert response.status_code == 200 and response.json() == {"updated": 1}
    _, query, update, _ = cards.writes[-1]
    assert query == {"_id": {"$in": [CARD_1]}, "tags": {"$in": ["verbs"]}}
    assert update["$pull"] == {"tags": {"$in": ["verbs"]}}


# ---------------------------------------------------------------------------
# 4. tag verbs — rename dedupes, remove reports its count
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rename_pulls_from_where_to_exists_and_rewrites_the_rest(monkeypatch):
    cards = FakeCollection(matched=4)
    response = await _cards_request("POST", "/study-cards/tags/rename", monkeypatch, cards, json={"from": "言葉できる3.4", "to": "言葉できる３.３"})

    assert response.status_code == 200
    assert response.json() == {"cards": 8}, "both writes count: 4 merged + 4 renamed"
    merge, rename = cards.writes
    assert merge[1] == {"user_id": OWNER, "deleted_at": None, "tags": {"$all": ["言葉できる3.4", "言葉できる３.３"]}}
    assert merge[2]["$pull"] == {"tags": "言葉できる3.4"}
    assert rename[1] == {"user_id": OWNER, "deleted_at": None, "tags": "言葉できる3.4"}
    assert rename[2]["$set"]["tags.$[el]"] == "言葉できる３.３"
    assert rename[3] == {"array_filters": [{"el": "言葉できる3.4"}]}


@pytest.mark.asyncio
async def test_rename_onto_itself_or_an_empty_tag_is_refused(monkeypatch):
    for body in ({"from": "a", "to": "a"}, {"from": "a", "to": "  "}, {"from_tag": "a", "to": "b", "extra": 1}):
        cards = FakeCollection()
        response = await _cards_request("POST", "/study-cards/tags/rename", monkeypatch, cards, json=body)
        assert response.status_code == 422, body
        assert cards.writes == []


@pytest.mark.asyncio
async def test_remove_pulls_from_every_active_card_and_reports_the_count(monkeypatch):
    cards = FakeCollection(matched=17)
    response = await _cards_request("POST", "/study-cards/tags/remove", monkeypatch, cards, json={"tag": "asia"})

    assert response.status_code == 200 and response.json() == {"cards": 17}
    kind, query, update, _ = cards.writes[-1]
    assert kind == "many" and query == {"user_id": OWNER, "deleted_at": None, "tags": "asia"}
    assert update["$pull"] == {"tags": "asia"} and "updated_at" in update["$set"]


# ---------------------------------------------------------------------------
# 5. the mark keeps one writer — bulk mark goes through _write_mark_many
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bulk_mark_and_unmark_go_through_the_mark_helper(monkeypatch):
    calls: list = []

    async def fake_write_mark_many(ids, collection, marked_at):
        calls.append((ids, marked_at))
        return len(ids)

    monkeypatch.setattr(study_cards, "_write_mark_many", fake_write_mark_many)
    cards = FakeCollection(find_rows=[{"_id": CARD_1, "deck_id": None}, {"_id": CARD_2, "deck_id": None}])

    marked = await _cards_request("POST", "/study-cards/bulk", monkeypatch, cards, json={"ids": [str(CARD_1), str(CARD_2), str(FOREIGN)], "action": "mark"})
    unmarked = await _cards_request("POST", "/study-cards/bulk", monkeypatch, cards, json={"ids": [str(CARD_1)], "action": "unmark"})

    assert marked.json() == {"updated": 2} and unmarked.json() == {"updated": 2}
    assert calls[0][0] == [CARD_1, CARD_2] and isinstance(calls[0][1], datetime)
    assert calls[1][1] is None
    assert cards.writes == [], "the route itself never writes marked_at"


@pytest.mark.asyncio
async def test_write_mark_many_sets_exactly_the_two_keys_the_single_writer_sets():
    cards = FakeCollection(matched=2)
    stamp = datetime(2026, 9, 6, 12, 0, 0)
    assert await study_cards._write_mark_many([CARD_1, CARD_2], cards, stamp) == 2
    kind, query, update, _ = cards.writes[-1]
    assert kind == "many" and query == {"_id": {"$in": [CARD_1, CARD_2]}}
    assert set(update) == {"$set"} and set(update["$set"]) == {"marked_at", "updated_at"}
    assert update["$set"]["marked_at"] == stamp
    assert await study_cards._write_mark_many([], cards, None) == 0 and len(cards.writes) == 1


# ---------------------------------------------------------------------------
# 6. the one clause — archived is out of every count
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_active_deck_or_excludes_archived_decks(monkeypatch):
    decks_fake = FakeCollection(find_rows=[{"_id": DECK_A}])
    monkeypatch.setattr(study_cards, "decks_collection", decks_fake)

    clause = await study_cards._active_deck_or(OWNER)

    assert decks_fake.queries[0] == {"user_id": OWNER, "deleted_at": None, "archived_at": None}
    assert clause[2] == {"deck_id": {"$in": [DECK_A, str(DECK_A)]}}


@pytest.mark.asyncio
async def test_statistics_counts_take_the_active_deck_scope(monkeypatch):
    cards = FakeCollection(find_rows=[])
    decks_fake = FakeCollection(find_rows=[{"_id": DECK_A}])
    response = await _cards_request("GET", "/study-cards/statistics", monkeypatch, cards, decks_fake=decks_fake)

    assert response.status_code == 200
    assert decks_fake.queries[0]["archived_at"] is None
    deck_or = [{"deck_id": None}, {"deck_id": {"$exists": False}}, {"deck_id": {"$in": [DECK_A, str(DECK_A)]}}]
    counts = [q for q in cards.queries if "$and" in q or "$or" in q]
    total, reviewed, due = counts
    assert total["$or"] == deck_or and "last_reviewed" not in total
    assert reviewed["$or"] == deck_or and reviewed["last_reviewed"] == {"$ne": None}
    assert due["$and"][0] == {"$or": deck_or}, "due today never counts an archived deck's card"
    assert due["$and"][1]["$or"][1] == {"next_review": None}


@pytest.mark.asyncio
async def test_tags_and_daily_review_read_archived_at(monkeypatch):
    decks_fake = FakeCollection(find_rows=[])
    await _cards_request("GET", "/study-cards/tags", monkeypatch, FakeCollection(), decks_fake=decks_fake)
    await _cards_request("GET", "/study-cards/daily-review", monkeypatch, FakeCollection(), decks_fake=decks_fake)
    assert all(q.get("archived_at", "missing") is None for q in decks_fake.queries), decks_fake.queries
    assert len(decks_fake.queries) == 2


# ---------------------------------------------------------------------------
# 7. archive / restore — a state, set and cleared
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archive_sets_the_stamp_and_the_status(monkeypatch):
    decks_fake = FakeCollection(find_one=lambda: _deck_doc(DECK_A, archived_at=None))
    response = await _decks_request("POST", f"/decks/{DECK_A}/archive", monkeypatch, decks_fake, FakeCollection())

    assert response.status_code == 200
    _, query, update, _ = decks_fake.writes[-1]
    assert query == {"_id": DECK_A}
    assert isinstance(update["$set"]["archived_at"], datetime)
    assert update["$set"]["status"] == "archived"
    assert set(update["$set"]) == {"archived_at", "status", "updated_at"}, "nothing else about the deck changes"


@pytest.mark.asyncio
async def test_restore_clears_the_stamp_and_derives_the_status(monkeypatch):
    stamp = datetime(2026, 9, 1)
    reviewed = FakeCollection(find_rows=[{"_id": CARD_1}])
    decks_fake = FakeCollection(find_one=lambda: _deck_doc(DECK_A, archived_at=stamp, status="archived"))
    response = await _decks_request("POST", f"/decks/{DECK_A}/restore", monkeypatch, decks_fake, reviewed)

    assert response.status_code == 200
    assert decks_fake.writes[-1][2]["$set"]["archived_at"] is None
    assert decks_fake.writes[-1][2]["$set"]["status"] == "review"
    assert reviewed.queries[0]["last_reviewed"] == {"$ne": None}
    assert reviewed.queries[0]["deck_id"] == {"$in": [DECK_A, str(DECK_A)]}

    decks_fake = FakeCollection(find_one=lambda: _deck_doc(DECK_A, archived_at=stamp, status="archived"))
    await _decks_request("POST", f"/decks/{DECK_A}/restore", monkeypatch, decks_fake, FakeCollection(find_rows=[]))
    assert decks_fake.writes[-1][2]["$set"]["status"] == "new"


@pytest.mark.asyncio
async def test_archive_is_owner_only(monkeypatch):
    decks_fake = FakeCollection(find_one=lambda: _deck_doc(DECK_A, user_id=OTHER))
    response = await _decks_request("POST", f"/decks/{DECK_A}/archive", monkeypatch, decks_fake, FakeCollection())
    assert response.status_code == 403
    assert decks_fake.writes == []


@pytest.mark.asyncio
async def test_deck_list_excludes_archived_by_default_and_lists_only_them_on_request(monkeypatch):
    decks_fake = FakeCollection(find_rows=[])
    await _decks_request("GET", "/decks", monkeypatch, decks_fake, FakeCollection())
    await _decks_request("GET", "/decks?archived=true&type=quiz", monkeypatch, decks_fake, FakeCollection())

    default, archived = decks_fake.queries
    assert default == {"user_id": OWNER, "deleted_at": None, "archived_at": None}, "also matches decks without the field"
    assert archived == {"user_id": OWNER, "deleted_at": None, "archived_at": {"$ne": None}, "deck_type": "quiz"}


def test_the_generic_deck_patch_cannot_set_archived_at():
    import ast
    import inspect

    source = inspect.getsource(decks.update_deck)
    assert '"archived_at"' in source, "archived_at must be popped from the untyped PATCH dict (ADR-023)"
    tree = ast.parse(inspect.getsource(decks))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert {"archive_deck", "restore_deck"} <= names


def test_deck_models_carry_archived_at():
    from app.models.Deck import Deck, DeckWithStats

    assert "archived_at" in Deck.model_fields and "archived_at" in DeckWithStats.model_fields
    assert DeckWithStats(name="d").archived_at is None
