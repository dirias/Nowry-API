"""BOARD-001 — boards v2 (ADR-047, docs/prd-boards.md D2, D5, D8, D10, FR-001).

Pinned here:

  1. The v1 → v2 migration builders map every v1 kind as D8 says: a sticky's
     title and body become one note, eight colours fold to five, a title node
     becomes text, a square an untitled frame behind, an entity an item (a
     retired priority through `migrated_to`, else a note); edges become links
     and an edge to a missing node is dropped.
  2. PATCH merges by id into what is stored, removes a deleted node's links,
     drops a dangling link, increments `rev`, enforces 300 changes / 500
     nodes / 1,000 links, re-reads and re-merges when another save landed in
     between, and answers 409 after three tries.
  3. The list carries counts and an outline, and summarises a v1 board without
     writing it; GET migrates a v1 board and writes it once.
  4. Duplicate (plan limit), delete (owner only, soft), PUT 410.
  5. Cards accept `source_board_id` and `source_board_node_id`.

Handlers are called directly with the `Depends()` values as kwargs; the router's
`db` is patched with a small in-memory collection that applies the filters it is
given, so the conditional write on `rev` is exercised for real.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

from app.models.Blackboard import BoardPatch, BoardToCardRequest, CreateBoardRequest  # noqa: E402
from app.routers import blackboards as router  # noqa: E402
from app.services import boards as svc  # noqa: E402

USER = "507f1f77bcf86cd799439011"
OTHER = "507f1f77bcf86cd799439022"
CALLER = {"user_id": USER}
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


# --- an in-memory collection that honours its filters ----------------------------


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict) and "$ne" in expected:
            if actual == expected["$ne"]:
                return False
        elif isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif isinstance(actual, list) and not isinstance(expected, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.length: Optional[int] = None

    async def to_list(self, length: Optional[int] = None) -> list[dict]:
        self.length = length
        return [dict(row) for row in self.rows][:length]


class FakeCollection:
    """`find_one`, `find`, `update_one`, `insert_one`, `count_documents`.

    `before_update` runs before each `update_one` — the hook a test uses to land
    another collaborator's save between this save's read and its write.
    """

    def __init__(self, docs: Optional[list[dict]] = None):
        self.docs: list[dict] = [dict(doc) for doc in docs or []]
        self.updates: list[tuple[dict, dict]] = []
        self.inserts: list[dict] = []
        self.find_queries: list[dict] = []
        self.before_update: Optional[Callable[["FakeCollection"], None]] = None

    async def find_one(self, query: dict) -> Optional[dict]:
        for doc in self.docs:
            if _matches(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict) -> FakeCursor:
        self.find_queries.append(query)
        return FakeCursor([doc for doc in self.docs if _matches(doc, query)])

    async def update_one(self, query: dict, update: dict) -> MagicMock:
        if self.before_update:
            self.before_update(self)
        self.updates.append((query, update))
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return MagicMock(matched_count=1)
        return MagicMock(matched_count=0)

    async def insert_one(self, doc: dict) -> MagicMock:
        stored = dict(doc, _id=ObjectId())
        self.docs.append(stored)
        self.inserts.append(stored)
        return MagicMock(inserted_id=stored["_id"])

    async def count_documents(self, query: dict) -> int:
        return sum(1 for doc in self.docs if _matches(doc, query))


def _db(boards: FakeCollection, priorities: Optional[FakeCollection] = None) -> MagicMock:
    mock_db = MagicMock()
    mock_db.blackboards = boards
    mock_db.priorities = priorities or FakeCollection()
    mock_db.users = FakeCollection()
    return mock_db


# --- fixtures ---------------------------------------------------------------------


def note(node_id: str, **fields: Any) -> dict:
    return svc.make_node(node_id, "note", fields.pop("x", 0), fields.pop("y", 0), **{"text": "", "color": "yellow", **fields})


def link(link_id: str, a: str, b: str, label: str = "") -> dict:
    return svc.make_link(link_id, a, b, label)


def v2_board(nodes: Optional[list] = None, links: Optional[list] = None, **fields: Any) -> dict:
    return {
        "_id": ObjectId(), "owner_user_id": USER, "user_id": USER, "collaborators": [],
        "name": "Photosynthesis", "schema": 2, "rev": 4, "nodes": nodes or [], "links": links or [],
        "viewport": {"x": 10, "y": 20, "zoom": 1.5}, "created_at": NOW, "updated_at": NOW,
        "deleted_at": None, **fields,
    }


PRIORITY_TASK = ObjectId()        # migrated to a task
PRIORITY_MILESTONE = ObjectId()   # migrated to a milestone on a goal
PRIORITY_UNMIGRATED = ObjectId()  # never migrated
GOAL_ID = "64b000000000000000000001"


def v1_board(**fields: Any) -> dict:
    """A v1 board with every kind the old canvas could save."""
    def entity(node_id: str, entity_type: str, entity_id: Any, title: str) -> dict:
        return {"id": node_id, "type": "entity", "position": {"x": 5, "y": 6},
                "data": {"entityType": entity_type, "entityId": str(entity_id), "title": title}}

    return {
        "_id": ObjectId(), "owner_user_id": USER, "user_id": USER, "collaborators": [],
        "name": "Old board", "viewport": {"x": 1, "y": 2, "zoom": 0.5},
        "nodes": [
            {"id": "s1", "type": "stickyNote", "position": {"x": 10, "y": 20}, "width": 200, "height": 150,
             "zIndex": 3, "data": {"title": " Light ", "body": "Chlorophyll absorbs it", "color": "teal", "emoji": "🌱"}},
            {"id": "s2", "type": "stickyNote", "position": {"x": 0, "y": 0}, "measured": {"width": 180, "height": 90},
             "data": {"title": "Only a title", "body": "", "color": "purple"}},
            {"id": "s3", "type": "stickyNote", "position": {"x": 0, "y": 0}, "style": {"width": 160, "height": 80},
             "data": {"title": "", "body": "", "color": "orange"}},
            {"id": "s4", "type": "stickyNote", "position": {"x": 0, "y": 0}, "data": {"title": "Red", "color": "red"}},
            {"id": "s5", "type": "stickyNote", "position": {"x": 0, "y": 0}, "data": {"body": "Slate body", "color": "slate"}},
            {"id": "t1", "type": "titleNode", "position": {"x": 1, "y": 2}, "data": {"label": "Chapter 3"}},
            {"id": "f1", "type": "squareNode", "position": {"x": -50, "y": -50}, "width": 640, "height": 480, "zIndex": 2, "data": {}},
            {"id": "f2", "type": "squareNode", "position": {"x": 0, "y": 0}, "data": {}},
            entity("e-task", "task", "64a000000000000000000001", "Read chapter"),
            entity("e-goal", "goal", GOAL_ID, "Pass biology"),
            entity("e-p1", "priority", PRIORITY_TASK, "Revise"),
            entity("e-p2", "priority", PRIORITY_MILESTONE, "Mock exam"),
            entity("e-p3", "priority", PRIORITY_UNMIGRATED, "Old priority"),
        ],
        "edges": [
            {"id": "x1", "source": "s1", "target": "t1", "label": "explains"},
            {"id": "x2", "source": "s2", "target": "e-goal", "data": {"label": "serves"}},
            {"id": "x3", "source": "s1", "target": "gone"},
        ],
        **fields,
    }


def priorities() -> FakeCollection:
    return FakeCollection([
        {"_id": PRIORITY_TASK, "migrated_to": {"type": "task", "id": "64a000000000000000000009"}},
        {"_id": PRIORITY_MILESTONE, "linked_entity_type": "goal", "linked_entity_id": GOAL_ID,
         "migrated_to": {"type": "milestone", "id": "m-1"}},
        {"_id": PRIORITY_UNMIGRATED, "linked_entity_type": "task", "linked_entity_id": "x"},
    ])


REFS = {
    str(PRIORITY_TASK): {"kind": "task", "id": "64a000000000000000000009", "goal_id": None},
    str(PRIORITY_MILESTONE): {"kind": "milestone", "id": "m-1", "goal_id": GOAL_ID},
    str(PRIORITY_UNMIGRATED): None,
}


# --- 1. the migration builders ---------------------------------------------------


def test_every_v1_kind_maps_as_d8_says():
    nodes, links = svc.migrate_board(v1_board(), REFS)
    by_id = {node["id"]: node for node in nodes}

    assert by_id["s1"] == svc.make_node("s1", "note", 10, 20, w=200.0, h=150.0, z=3,
                                        text="Light\nChlorophyll absorbs it", color="green")
    assert (by_id["s2"]["text"], by_id["s2"]["color"], by_id["s2"]["w"], by_id["s2"]["h"]) == ("Only a title", "blue", 180.0, 90.0)
    assert (by_id["s3"]["text"], by_id["s3"]["color"], by_id["s3"]["w"]) == ("", "yellow", 160.0)
    assert (by_id["s4"]["color"], by_id["s4"]["w"], by_id["s4"]["h"]) == ("pink", None, None)
    assert (by_id["s5"]["text"], by_id["s5"]["color"]) == ("Slate body", "grey")
    assert by_id["t1"] == svc.make_node("t1", "text", 1, 2, text="Chapter 3", size="lg")
    assert by_id["f1"] == svc.make_node("f1", "frame", -50, -50, title="", w=640.0, h=480.0, z=-1)
    assert (by_id["f2"]["w"], by_id["f2"]["h"], by_id["f2"]["z"]) == (300.0, 300.0, -1)
    assert by_id["e-task"]["kind"] == "item"
    assert by_id["e-task"]["ref"] == {"kind": "task", "id": "64a000000000000000000001", "goal_id": None}
    assert by_id["e-goal"]["ref"] == {"kind": "goal", "id": GOAL_ID, "goal_id": None}
    assert by_id["e-p1"]["ref"] == {"kind": "task", "id": "64a000000000000000000009", "goal_id": None}
    assert by_id["e-p2"]["ref"] == {"kind": "milestone", "id": "m-1", "goal_id": GOAL_ID}
    assert (by_id["e-p3"]["kind"], by_id["e-p3"]["text"], by_id["e-p3"]["ref"]) == ("note", "Old priority", None)
    assert all("emoji" not in node for node in nodes)
    assert [node["id"] for node in nodes] == [node["id"] for node in v1_board()["nodes"]]

    assert links == [link("x1", "s1", "t1", "explains"), link("x2", "s2", "e-goal", "serves")]


def test_priority_ref_needs_a_goal_for_a_milestone_and_a_stamp_at_all():
    assert svc.priority_ref({"migrated_to": {"type": "task", "id": "t"}}) == {"kind": "task", "id": "t", "goal_id": None}
    assert svc.priority_ref({"migrated_to": {"type": "milestone", "id": "m"}, "linked_entity_type": "task"}) is None
    assert svc.priority_ref({"linked_entity_type": "goal", "linked_entity_id": "g"}) is None
    assert svc.priority_ids([v1_board(), v2_board()]) == [str(PRIORITY_TASK), str(PRIORITY_MILESTONE), str(PRIORITY_UNMIGRATED)]


def test_migrated_nodes_and_links_validate_as_the_wire_models():
    nodes, links = svc.migrate_board(v1_board(), REFS)
    response = router._board_response({**v1_board(), "schema": 2, "rev": 1, "nodes": nodes, "links": links})
    assert len(response.nodes) == 13 and response.links[0].from_id == "s1"


# --- 2. PATCH by change -------------------------------------------------------------


async def _patch(boards: FakeCollection, board: dict, **body: Any):
    with patch.object(router, "db", _db(boards)):
        return await router.patch_board(board_id=str(board["_id"]), body=BoardPatch(**body), current_user=CALLER)


@pytest.mark.asyncio
async def test_patch_merges_by_id_and_increments_the_revision():
    board = v2_board([note("a", text="A"), note("b", text="B"), note("c")],
                     [link("ab", "a", "b"), link("bc", "b", "c"), link("ca", "c", "a")])
    boards = FakeCollection([board])

    result = await _patch(
        boards, board, rev=4, name="Light reactions", viewport={"x": 0, "y": 0, "zoom": 2},
        upsert_nodes=[{"id": "a", "kind": "note", "x": 5, "y": 5, "text": "A2", "color": "blue"},
                      {"id": "d", "kind": "text", "x": 9, "y": 9, "text": "Title", "size": "xl"}],
        delete_nodes=["c"],
        upsert_links=[{"id": "ad", "from": "a", "to": "d", "label": "then"},
                      {"id": "dangling", "from": "a", "to": "nowhere"}],
        delete_links=["ab"],
    )

    stored = boards.docs[0]
    assert result.rev == 5 and stored["rev"] == 5
    assert [node["id"] for node in stored["nodes"]] == ["a", "b", "d"]
    assert stored["nodes"][0]["text"] == "A2" and stored["nodes"][0]["color"] == "blue"
    # Deleting c took bc and ca with it; ab was deleted; the dangling link never landed.
    assert stored["links"] == [link("ad", "a", "d", "then")]
    assert stored["name"] == "Light reactions"
    assert stored["viewport"] == {"x": 0.0, "y": 0.0, "zoom": 2.0}
    query, _ = boards.updates[0]
    assert query["rev"] == 4 and query["_id"] == board["_id"]


@pytest.mark.asyncio
async def test_patch_rev_is_informational_the_server_always_merges():
    board = v2_board([note("a")], rev=9)
    boards = FakeCollection([board])
    result = await _patch(boards, board, rev=2, upsert_nodes=[{"id": "b", "kind": "note", "x": 0, "y": 0}])
    assert result.rev == 10
    assert [node["id"] for node in boards.docs[0]["nodes"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_patch_refuses_more_than_300_changes_before_reading():
    board = v2_board()
    boards = FakeCollection([board])
    with pytest.raises(HTTPException) as exc:
        await _patch(boards, board, rev=4, delete_nodes=[f"n{i}" for i in range(200)],
                     delete_links=[f"l{i}" for i in range(101)])
    assert (exc.value.status_code, exc.value.detail) == (422, "too_many_changes")
    assert boards.updates == []


@pytest.mark.asyncio
async def test_patch_refuses_a_board_over_500_nodes_or_1000_links_and_writes_nothing():
    board = v2_board([note(f"n{i}") for i in range(450)])
    boards = FakeCollection([board])
    with pytest.raises(HTTPException) as exc:
        await _patch(boards, board, rev=4, upsert_nodes=[{"id": f"m{i}", "kind": "note", "x": 0, "y": 0} for i in range(51)])
    assert (exc.value.status_code, exc.value.detail) == (422, "board_too_large")
    assert boards.updates == []

    nodes = [note("a"), note("b")]
    board = v2_board(nodes, [link(f"l{i}", "a", "b") for i in range(990)])
    boards = FakeCollection([board])
    with pytest.raises(HTTPException) as exc:
        await _patch(boards, board, rev=4, upsert_links=[{"id": f"k{i}", "from": "a", "to": "b"} for i in range(11)])
    assert exc.value.detail == "board_too_large" and boards.updates == []


def test_a_board_already_over_the_limit_can_still_be_edited_down():
    before = ([note(f"n{i}") for i in range(600)], [])
    svc.check_size(before, (before[0][:-1], []))  # shrinking: allowed
    with pytest.raises(HTTPException):
        svc.check_size(before, (before[0] + [note("x")], []))


@pytest.mark.asyncio
async def test_patch_rereads_and_remerges_when_another_save_lands_in_between():
    board = v2_board([note("a", text="A"), note("b", text="B")])
    boards = FakeCollection([board])

    def other_collaborator_saves(collection: FakeCollection) -> None:
        collection.before_update = None
        stored = collection.docs[0]
        stored["nodes"] = [note("a", text="A"), note("b", text="B by Sam")]
        stored["rev"] = 5

    boards.before_update = other_collaborator_saves
    result = await _patch(boards, board, rev=4, upsert_nodes=[{"id": "a", "kind": "note", "x": 1, "y": 1, "text": "A by me"}])

    stored = boards.docs[0]
    assert result.rev == 6 and stored["rev"] == 6
    assert [node["text"] for node in stored["nodes"]] == ["A by me", "B by Sam"]  # both kept
    assert [query["rev"] for query, _ in boards.updates] == [4, 5]


@pytest.mark.asyncio
async def test_patch_gives_up_with_409_after_three_tries():
    board = v2_board([note("a")])
    boards = FakeCollection([board])

    def someone_always_saves_first(collection: FakeCollection) -> None:
        collection.docs[0]["rev"] += 1

    boards.before_update = someone_always_saves_first
    with pytest.raises(HTTPException) as exc:
        await _patch(boards, board, rev=4, upsert_nodes=[{"id": "b", "kind": "note", "x": 0, "y": 0}])
    assert (exc.value.status_code, exc.value.detail) == (409, "board_busy")
    assert len(boards.updates) == router.SAVE_TRIES


@pytest.mark.asyncio
async def test_patch_guards_owner_and_collaborators():
    shared = v2_board([note("a")], owner_user_id=OTHER, user_id=OTHER, collaborators=[USER])
    boards = FakeCollection([shared])
    assert (await _patch(boards, shared, rev=4, name="Mine too")).rev == 5

    foreign = v2_board(owner_user_id=OTHER, user_id=OTHER)
    boards = FakeCollection([foreign])
    with pytest.raises(HTTPException) as exc:
        await _patch(boards, foreign, rev=4, name="x")
    assert exc.value.status_code == 403 and boards.updates == []


@pytest.mark.asyncio
async def test_a_legacy_board_is_the_legacy_users_alone():
    legacy = v2_board(owner_user_id=None, user_id=USER, board_id="main")
    boards = FakeCollection([legacy])
    with patch.object(router, "db", _db(boards)):
        result = await router.patch_board(board_id="main", body=BoardPatch(rev=4, name="x"), current_user=CALLER)
        assert result.rev == 5
        with pytest.raises(HTTPException) as exc:
            await router.patch_board(board_id="main", body=BoardPatch(rev=4, name="x"), current_user={"user_id": OTHER})
    assert exc.value.status_code == 404  # another user's "main" is not even found


@pytest.mark.asyncio
async def test_patch_on_a_v1_board_migrates_and_merges():
    board = v1_board()
    boards = FakeCollection([board])
    with patch.object(router, "db", _db(boards, priorities())):
        result = await router.patch_board(
            board_id=str(board["_id"]), body=BoardPatch(rev=0, delete_nodes=["t1"]), current_user=CALLER)
    stored = boards.docs[0]
    assert result.rev == 2 and stored["schema"] == 2 and "edges" not in stored
    assert "t1" not in [node["id"] for node in stored["nodes"]]
    assert stored["links"] == [link("x2", "s2", "e-goal", "serves")]


def test_patch_body_refuses_a_whole_document():
    with pytest.raises(ValidationError):
        BoardPatch(rev=1, nodes=[])
    with pytest.raises(ValidationError):
        BoardPatch(rev=1, upsert_nodes=[{"id": "a", "kind": "note", "x": 0, "y": 0, "color": "purple"}])


# --- 3. list and GET ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_carries_counts_and_an_outline_and_leaves_a_v1_board_unwritten():
    frame = svc.make_node("f", "frame", 0, 0, title="Group", w=500.0, h=400.0, z=-1)
    item = svc.make_node("i", "item", 30, 40, ref={"kind": "task", "id": "t", "goal_id": None})
    text = svc.make_node("t", "text", 50, 60, text="Heading", size="lg", z=2)
    nodes = [note("a", card_ids=["c1", "c2"], color="pink"), text, item, frame, note("b", card_ids=["c3"], z=-3)]
    v2 = v2_board(nodes, [link("ab", "a", "b")])
    v1 = v1_board()
    boards = FakeCollection([v2, v1])
    prio = priorities()

    with patch.object(router, "db", _db(boards, prio)):
        result = await router.list_boards(current_user=CALLER)

    row = result[0]
    assert row.counts.model_dump() == {"notes": 2, "links": 1, "items": 1, "cards": 3}
    assert [(rect.kind, rect.x, rect.w, rect.h) for rect in row.outline] == [
        ("frame", 0.0, 500.0, 400.0),     # frames first
        ("note", 0.0, 220.0, 100.0),      # then by z (b is -3) ...
        ("note", 0.0, 220.0, 100.0),      # ... then insertion
        ("item", 30.0, 256.0, 56.0),
        ("text", 50.0, 240.0, 40.0),
    ]
    assert row.outline[2].color == "pink" and row.is_owner is True

    old = result[1]
    assert old.counts.model_dump() == {"notes": 6, "links": 2, "items": 4, "cards": 0}
    assert old.outline[0].kind == "frame"
    assert boards.updates == []                                  # summarised, not written
    assert len(prio.find_queries) == 1                           # one priority lookup for the list
    assert [query for query in boards.find_queries] == [
        {"owner_user_id": USER, "deleted_at": None}, {"collaborators": USER, "deleted_at": None}]


def test_the_outline_is_at_most_sixty_rectangles():
    assert len(svc.outline([note(f"n{i}") for i in range(80)])) == 60


@pytest.mark.asyncio
async def test_get_migrates_a_v1_board_and_writes_it_once():
    board = v1_board()
    boards = FakeCollection([board])
    with patch.object(router, "db", _db(boards, priorities())):
        first = await router.get_blackboard(board_id=str(board["_id"]), current_user=CALLER)
        second = await router.get_blackboard(board_id=str(board["_id"]), current_user=CALLER)

    assert len(boards.updates) == 1
    query, update = boards.updates[0]
    assert query == {"_id": board["_id"], "schema": {"$ne": 2}}
    assert update["$set"]["schema"] == 2 and update["$set"]["rev"] == 1 and update["$unset"] == {"edges": ""}
    stored = boards.docs[0]
    assert "edges" not in stored and stored["nodes"][0]["kind"] == "note"
    assert first.model_dump(by_alias=True) == second.model_dump(by_alias=True)
    assert first.schema_version == 2 and first.rev == 1
    assert first.viewport.model_dump() == {"x": 1.0, "y": 2.0, "zoom": 0.5}


@pytest.mark.asyncio
async def test_get_excludes_a_soft_deleted_board():
    board = v2_board(deleted_at=NOW)
    with patch.object(router, "db", _db(FakeCollection([board]))):
        with pytest.raises(HTTPException) as exc:
            await router.get_blackboard(board_id=str(board["_id"]), current_user=CALLER)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_wire_shape_over_http():
    board = v2_board([note("a", text="A"), note("b")], [link("ab", "a", "b", "why")])
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_firebase_user] = lambda: CALLER
    try:
        with patch.object(router, "db", _db(FakeCollection([board]))):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                got = (await client.get(f"/blackboards/{board['_id']}")).json()
                patched = await client.patch(f"/blackboards/{board['_id']}", json={
                    "rev": 4, "upsert_links": [{"id": "ba", "from": "b", "to": "a"}]})
                put = await client.put(f"/blackboards/{board['_id']}", json={"nodes": []})
    finally:
        app.dependency_overrides.clear()

    assert set(got) == {"id", "name", "owner_user_id", "owner_name", "collaborators", "collaborator_names",
                        "schema", "rev", "nodes", "links", "viewport", "created_at", "updated_at"}
    assert got["schema"] == 2 and got["links"] == [{"id": "ab", "from": "a", "to": "b", "label": "why"}]
    assert set(got["nodes"][0]) == {"id", "kind", "x", "y", "w", "h", "z", "text", "color", "size", "title", "ref", "card_ids"}
    assert patched.status_code == 200 and set(patched.json()) == {"rev", "updated_at"} and patched.json()["rev"] == 5
    assert put.status_code == 410 and put.json()["detail"] == router.BOARDS_GONE


# --- 4. create, duplicate, delete, PUT ------------------------------------------------


@pytest.mark.asyncio
async def test_create_makes_a_v2_board():
    boards = FakeCollection()
    with patch.object(router, "db", _db(boards)):
        result = await router.create_board(body=CreateBoardRequest(name="New"), current_user=CALLER, tier="free")
    stored = boards.inserts[0]
    assert (stored["schema"], stored["rev"], stored["nodes"], stored["links"]) == (2, 1, [], [])
    assert "edges" not in stored
    assert result.rev == 1 and result.owner_user_id == USER and result.nodes == []


@pytest.mark.asyncio
async def test_a_deleted_board_frees_the_plan_slot():
    boards = FakeCollection([v2_board(deleted_at=NOW)])
    with patch.object(router, "db", _db(boards)):
        await router.create_board(body=CreateBoardRequest(name="Again"), current_user=CALLER, tier="free")
    assert len(boards.inserts) == 1


@pytest.mark.asyncio
async def test_duplicate_copies_for_the_caller_alone():
    source = v2_board([note("a", text="A")], [], owner_user_id=OTHER, user_id=OTHER,
                      collaborators=[USER], name="x" * 100)
    boards = FakeCollection([source])
    with patch.object(router, "db", _db(boards)):
        result = await router.duplicate_board(board_id=str(source["_id"]), current_user=CALLER, tier="pro")

    copy = boards.inserts[0]
    assert copy["owner_user_id"] == USER and copy["collaborators"] == []
    assert copy["name"] == "x" * 93 + " (copy)" and len(copy["name"]) == 100
    assert copy["nodes"] == source["nodes"] and copy["viewport"] == {"x": 10.0, "y": 20.0, "zoom": 1.5}
    assert (copy["schema"], copy["rev"]) == (2, 1)
    assert result.id == str(copy["_id"]) and result.nodes[0].text == "A"
    assert svc.copy_name("Biology") == "Biology (copy)"


@pytest.mark.asyncio
async def test_duplicate_counts_against_the_plan():
    mine = v2_board()
    boards = FakeCollection([mine])
    with patch.object(router, "db", _db(boards)):
        with pytest.raises(HTTPException) as exc:
            await router.duplicate_board(board_id=str(mine["_id"]), current_user=CALLER, tier="free")
    assert (exc.value.status_code, exc.value.detail) == (402, "board_limit_reached")
    assert boards.inserts == []


@pytest.mark.asyncio
async def test_duplicate_of_a_v1_board_copies_it_as_v2():
    source = v1_board()
    boards = FakeCollection([source])
    with patch.object(router, "db", _db(boards, priorities())):
        await router.duplicate_board(board_id=str(source["_id"]), current_user=CALLER, tier="pro")
    copy = boards.inserts[0]
    assert len(copy["nodes"]) == 13 and len(copy["links"]) == 2 and "edges" not in copy


@pytest.mark.asyncio
async def test_delete_is_the_owners_and_soft():
    board = v2_board(collaborators=[OTHER])
    boards = FakeCollection([board])
    with patch.object(router, "db", _db(boards)):
        with pytest.raises(HTTPException) as exc:
            await router.delete_board(board_id=str(board["_id"]), current_user={"user_id": OTHER})
        assert exc.value.status_code == 403 and boards.updates == []

        assert await router.delete_board(board_id=str(board["_id"]), current_user=CALLER) == {"ok": True}
        stored = boards.docs[0]
        assert stored["deleted_at"] is not None and stored["deleted_by"] == USER
        assert stored["nodes"] == board["nodes"]                   # kept for the recovery window
        assert await router.list_boards(current_user=CALLER) == []
        with pytest.raises(HTTPException) as gone:
            await router.get_blackboard(board_id=str(board["_id"]), current_user=CALLER)
    assert gone.value.status_code == 404


@pytest.mark.asyncio
async def test_put_answers_410():
    with pytest.raises(HTTPException) as exc:
        await router.save_blackboard(board_id=str(ObjectId()), current_user=CALLER)
    assert exc.value.status_code == 410 and "PATCH" in exc.value.detail


# --- generate-cards reads a v2 board -----------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cards_reads_the_stored_v2_nodes_when_no_texts_are_sent():
    frame = svc.make_node("f", "frame", 0, 0, title="Calvin cycle", w=300.0, h=300.0, z=-1)
    item = svc.make_node("i", "item", 0, 0, ref={"kind": "task", "id": "t", "goal_id": None})
    board = v2_board([note("a", text="Light reactions make ATP"), frame, item])
    fake_invoke = MagicMock(return_value={"generated_cards": [{"front": "Q", "back": "A"}]})

    with patch.object(router, "db", _db(FakeCollection([board]))), \
         patch.object(router.orchestrator, "invoke", fake_invoke):
        result = await router.generate_cards_from_board(
            board_id=str(board["_id"]), body=BoardToCardRequest(node_ids=["a", "f", "i"]),
            current_user=CALLER, tier="pro")

    sample = fake_invoke.call_args.args[1]["sampleText"]
    assert "Node 1: Light reactions make ATP" in sample and "Node 2: Calvin cycle" in sample
    assert "Node 3" not in sample                                    # the item gave nothing
    assert result.node_count == 3


def test_card_texts_by_kind():
    text = svc.make_node("t", "text", 0, 0, text="Heading", size="lg")
    assert svc.selection_texts([note("a", text="A"), text], ["t", "missing", "a"]) == ["Heading", "A"]


# --- 5. cards keep where on a board they came from ----------------------------------------


def test_card_source_fields_exist_and_are_not_patchable():
    from app.models.StudyCard import StudyCard, StudyCardUpdate

    card = StudyCard(title="Q", content="A", source_board_id="b1", source_board_node_id="n1")
    dumped = card.model_dump(by_alias=True, exclude={"id"})
    assert (dumped["source_board_id"], dumped["source_board_node_id"]) == ("b1", "n1")
    assert StudyCard(title="Q", content="A").source_board_id is None
    assert not ({"source_board_id", "source_board_node_id"} & set(StudyCardUpdate.model_fields))


@pytest.mark.asyncio
async def test_the_card_create_route_stores_the_board_source():
    from app.models.StudyCard import StudyCard
    from app.routers import study_cards

    stored: dict = {}

    async def insert_one(doc: dict) -> MagicMock:
        stored.update(doc, _id=ObjectId())
        return MagicMock(inserted_id=stored["_id"])

    cards = MagicMock()
    cards.count_documents = AsyncMock(return_value=0)
    cards.insert_one = insert_one
    cards.find_one = AsyncMock(side_effect=lambda query: dict(stored))
    users = MagicMock()
    users.find_one = AsyncMock(return_value={"_id": ObjectId(USER), "subscription": {"tier": "pro"}})

    with patch("app.config.database.users_collection", users), \
         patch.object(study_cards, "sync_tag_rests", AsyncMock()):
        result = await study_cards.create_study_card(
            card=StudyCard(title="Q", content="A", source_board_id="b1", source_board_node_id="n1"),
            collection=cards, d_collection=MagicMock(), user=CALLER)

    assert stored["source_board_id"] == "b1" and stored["source_board_node_id"] == "n1"
    assert result["source_board_id"] == "b1"
