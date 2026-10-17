"""BOARD-012 and BOARD-013 — the API halves.

BOARD-012: the board list and the single board carry `collaborator_names`
({user id: name}) beside `owner_name`, resolved for owners and collaborators
together by one bounded users query (at most 500 ids). Only boards the caller
can open answer at all, so the names never reach a stranger.

BOARD-013: `GET /annual-plan/goals/{goal_id}` answers one goal with its
milestones, whichever year it belongs to; another user's goal is refused and a
soft-deleted goal is 404. It shadows neither the goal list nor the activities.

Handlers are called directly with the `Depends()` values as kwargs; the
collections are small in-memory fakes that apply the filters they are given.
"""
from __future__ import annotations

import sys
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.routing import APIRoute

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

from app.models.Blackboard import CreateBoardRequest  # noqa: E402
from app.routers import annual_planning as plans  # noqa: E402
from app.routers import blackboards as boards  # noqa: E402
from app.services import boards as svc  # noqa: E402

OWNER = "507f1f77bcf86cd799439011"
ALICE = "507f1f77bcf86cd799439022"
BOB = "507f1f77bcf86cd799439033"
GHOST = "507f1f77bcf86cd799439044"   # a collaborator whose account is gone
STRANGER = "507f1f77bcf86cd799439055"


# --- an in-memory collection that honours its filters ----------------------------


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict) and "$in" in expected:
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
    def __init__(self, docs: Optional[list[dict]] = None):
        self.docs = [dict(doc) for doc in docs or []]
        self.find_queries: list[dict] = []
        self.cursors: list[FakeCursor] = []

    async def find_one(self, query: dict) -> Optional[dict]:
        return next((dict(doc) for doc in self.docs if _matches(doc, query)), None)

    def find(self, query: dict) -> FakeCursor:
        self.find_queries.append(query)
        cursor = FakeCursor([doc for doc in self.docs if _matches(doc, query)])
        self.cursors.append(cursor)
        return cursor

    async def insert_one(self, doc: dict) -> MagicMock:
        stored = dict(doc, _id=ObjectId())
        self.docs.append(stored)
        return MagicMock(inserted_id=stored["_id"])

    async def count_documents(self, query: dict) -> int:
        return sum(1 for doc in self.docs if _matches(doc, query))


def _user(user_id: str, username: str = "", email: str = "") -> dict:
    return {"_id": ObjectId(user_id), "username": username, "email": email}


USERS = [
    _user(OWNER, username="olivia"),
    _user(ALICE, username="alice", email="alice@example.com"),
    _user(BOB, email="bob@example.com"),        # no username: the email is the name
    _user(STRANGER, username="sam"),
]


def _board(owner: str = OWNER, collaborators: Optional[list[str]] = None, name: str = "Plans") -> dict:
    return {
        "_id": ObjectId(), "owner_user_id": owner, "user_id": owner,
        "collaborators": list(collaborators or []), "name": name,
        "schema": svc.SCHEMA, "rev": 1, "nodes": [], "links": [],
        "viewport": dict(svc.DEFAULT_VIEWPORT), "deleted_at": None,
    }


def _boards_db(stored: list[dict], users: Optional[list[dict]] = None) -> MagicMock:
    mock_db = MagicMock()
    mock_db.blackboards = FakeCollection(stored)
    mock_db.priorities = FakeCollection()
    mock_db.users = FakeCollection(USERS if users is None else users)
    return mock_db


# --- BOARD-012: the list -----------------------------------------------------------


async def test_the_list_names_owners_and_collaborators_in_one_bounded_query():
    shared = _board(collaborators=[ALICE, BOB, GHOST])
    own = _board(owner=ALICE, collaborators=[OWNER], name="Alice's")
    mock_db = _boards_db([shared, own])
    with patch.object(boards, "db", mock_db):
        rows = await boards.list_boards(current_user={"user_id": ALICE})

    by_name = {row.name: row for row in rows}
    assert by_name["Plans"].owner_name == "olivia"
    # Alice herself is named too; the gone account is left out, not "".
    assert by_name["Plans"].collaborator_names == {ALICE: "alice", BOB: "bob@example.com"}
    assert by_name["Alice's"].owner_name == "alice"
    assert by_name["Alice's"].collaborator_names == {OWNER: "olivia"}

    assert len(mock_db.users.find_queries) == 1
    asked = mock_db.users.find_queries[0]["_id"]["$in"]
    assert sorted(map(str, asked)) == sorted([OWNER, ALICE, BOB, GHOST])
    assert mock_db.users.cursors[0].length == len(asked)


async def test_the_lookup_is_capped_at_500_ids_owners_first():
    crowd = [str(ObjectId()) for _ in range(600)]
    mock_db = _boards_db([_board(owner=STRANGER, collaborators=[OWNER, *crowd])])
    with patch.object(boards, "db", mock_db):
        await boards.list_boards(current_user={"user_id": OWNER})

    asked = mock_db.users.find_queries[0]["_id"]["$in"]
    assert len(asked) == boards.NAME_LOOKUP_CAP == 500
    assert asked[0] == ObjectId(STRANGER)
    assert mock_db.users.cursors[0].length == 500


async def test_a_board_with_invalid_ids_issues_no_lookup():
    mock_db = _boards_db([_board(owner="legacy-uid", collaborators=["not-an-id"])])
    with patch.object(boards, "db", mock_db):
        rows = await boards.list_boards(current_user={"user_id": "legacy-uid"})

    assert rows[0].owner_name == ""
    assert rows[0].collaborator_names == {}
    assert mock_db.users.find_queries == []


# --- BOARD-012: one board ------------------------------------------------------------


async def test_a_collaborator_reads_the_owners_and_the_other_collaborators_names():
    board = _board(collaborators=[ALICE, BOB, GHOST])
    with patch.object(boards, "db", _boards_db([board])):
        result = await boards.get_blackboard(board_id=str(board["_id"]), current_user={"user_id": BOB})

    assert result.owner_name == "olivia"
    assert result.collaborator_names == {ALICE: "alice", BOB: "bob@example.com"}
    wire = result.model_dump(mode="json")
    assert wire["collaborator_names"] == {ALICE: "alice", BOB: "bob@example.com"}


async def test_a_stranger_gets_403_and_no_names():
    board = _board(collaborators=[ALICE])
    mock_db = _boards_db([board])
    with patch.object(boards, "db", mock_db):
        with pytest.raises(HTTPException) as refused:
            await boards.get_blackboard(board_id=str(board["_id"]), current_user={"user_id": STRANGER})

    assert refused.value.status_code == 403
    assert mock_db.users.find_queries == []


async def test_an_unknown_board_is_404():
    with patch.object(boards, "db", _boards_db([])):
        with pytest.raises(HTTPException) as missing:
            await boards.get_blackboard(board_id=str(ObjectId()), current_user={"user_id": OWNER})

    assert missing.value.status_code == 404


async def test_create_and_duplicate_answer_empty_collaborator_names():
    board = _board(collaborators=[ALICE])
    with patch.object(boards, "db", _boards_db([board])):
        created = await boards.create_board(
            body=CreateBoardRequest(name="Fresh"), current_user={"user_id": OWNER}, tier="pro",
        )
        copied = await boards.duplicate_board(
            board_id=str(board["_id"]), current_user={"user_id": OWNER}, tier="pro",
        )

    for fresh in (created, copied):
        assert fresh.collaborators == []
        assert fresh.collaborator_names == {}


# --- BOARD-013: one goal by id ---------------------------------------------------------

PLAN_ID = ObjectId()
FA_ID = str(ObjectId())
GOAL_ID = ObjectId()
MILESTONE = {"id": "m1", "title": "Half marathon", "due_date": "2025-06-01",
             "completed": True, "is_key_result": True, "is_priority": False}


def _goal(**fields) -> dict:
    return {"_id": GOAL_ID, "focus_area_id": FA_ID, "title": "Run 10k", "year": 2025,
            "type": "annual", "milestones": [MILESTONE], "deleted_at": None, **fields}


def _plan_collections(goal: dict) -> tuple:
    return (
        patch.object(plans, "goals_collection", FakeCollection([goal])),
        patch.object(plans, "focus_areas_collection",
                     FakeCollection([{"_id": ObjectId(FA_ID), "annual_plan_id": str(PLAN_ID)}])),
        patch.object(plans, "annual_plans_collection",
                     FakeCollection([{"_id": PLAN_ID, "user_id": OWNER, "year": 2025}])),
    )


async def _get_goal(goal: dict, goal_id: str, user_id: str):
    goals_p, areas_p, plans_p = _plan_collections(goal)
    with goals_p, areas_p, plans_p:
        return await plans.get_goal(goal_id=goal_id, current_user={"user_id": user_id})


async def test_the_owner_reads_last_years_goal_with_its_milestones():
    doc = await _get_goal(_goal(), str(GOAL_ID), OWNER)
    goal = plans.Goal.model_validate(doc)

    assert str(goal.id) == str(GOAL_ID)
    assert goal.year == 2025
    assert [milestone.model_dump() for milestone in goal.milestones] == [MILESTONE]


async def test_another_users_goal_is_refused():
    with pytest.raises(HTTPException) as refused:
        await _get_goal(_goal(), str(GOAL_ID), STRANGER)

    assert refused.value.status_code == 403


async def test_a_deleted_goal_is_404():
    with pytest.raises(HTTPException) as missing:
        await _get_goal(_goal(deleted_at="2026-10-01T00:00:00Z"), str(GOAL_ID), OWNER)

    assert missing.value.status_code == 404


@pytest.mark.parametrize("goal_id", [str(ObjectId()), "not-an-object-id"])
async def test_an_unknown_or_invalid_id_is_404(goal_id: str):
    with pytest.raises(HTTPException) as missing:
        await _get_goal(_goal(), goal_id, OWNER)

    assert missing.value.status_code == 404


@pytest.mark.parametrize("path, endpoint", [
    ("/annual-plan/goals", "get_goals"),
    ("/annual-plan/goals/abc", "get_goal"),
    ("/annual-plan/goals/abc/activities", "get_activities"),
])
def test_the_route_shadows_neither_the_list_nor_the_activities(path: str, endpoint: str):
    gets = [route for route in plans.router.routes
            if isinstance(route, APIRoute) and "GET" in route.methods]
    first = next(route for route in gets if route.path_regex.match(path))

    assert first.endpoint.__name__ == endpoint
