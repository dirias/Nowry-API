"""
PLAN-010 — the calendar's bugs on the API side.

(4) ``PUT /annual-plan/activities/{id}`` writes only the fields the body
    carries. It used to validate the body as a full ``Activity`` and ``$set``
    the model's defaults, so the calendar's ``{title}`` rename reset the
    schedule; and it dropped ``description`` and ``duration_minutes`` outright.
(5) ``DELETE focus-areas/{id}`` and ``DELETE goals/{id}`` own through the plan.
    They filtered on a ``user_id`` neither document carries, so every delete
    was a 404 — the owner's included.

Harness and stubs mirror tests/test_annual_planning_be.py.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from tests._stubs import use_stub_if_missing

_mock_firebase = MagicMock()
_mock_firebase.get_firebase_user = MagicMock()
use_stub_if_missing("app.auth.firebase_auth", _mock_firebase)
sys.modules.setdefault("app.config.database", MagicMock())

ROUTER = "app.routers.annual_planning"
PLAN_ID = "507f1f77bcf86cd799439001"
USER_ID = "507f1f77bcf86cd799439011"  # conftest's mock_firebase_user
OTHER_USER_ID = "507f1f77bcf86cd799439099"
FA_ID = str(ObjectId())
GOAL_ID = str(ObjectId())


def _plan(user_id: str = USER_ID) -> dict:
    return {"_id": ObjectId(PLAN_ID), "user_id": user_id, "year": 2026, "deleted_at": None}


def _focus_area() -> dict:
    return {"_id": ObjectId(FA_ID), "annual_plan_id": PLAN_ID, "name": "Health", "deleted_at": None}


def _goal() -> dict:
    return {"_id": ObjectId(GOAL_ID), "focus_area_id": FA_ID, "title": "Run 10k", "deleted_at": None}


def _matched(count: int = 1) -> MagicMock:
    result = MagicMock()
    result.matched_count = count
    return result


def _set_of(collection: MagicMock) -> dict:
    """The ``$set`` document of the collection's one ``update_one`` call."""
    return collection.update_one.call_args[0][1]["$set"]


def _collections():
    """Every collection the three routes touch, patched at once."""
    return (
        patch(f"{ROUTER}.activities_collection"),
        patch(f"{ROUTER}.goals_collection"),
        patch(f"{ROUTER}.focus_areas_collection"),
        patch(f"{ROUTER}.annual_plans_collection"),
        patch(f"{ROUTER}.priorities_collection"),
    )


# ---------------------------------------------------------------------------
# (4) PUT /activities/{id} — a partial body
# ---------------------------------------------------------------------------

def _owned_activity() -> dict:
    return {
        "_id": ObjectId(),
        "goal_id": GOAL_ID,
        "title": "Stretch",
        "frequency": "weekly",
        "days_of_week": [0, 2, 4],
        "time_of_day": "morning",
        "duration_minutes": 45,
        "is_active": True,
    }


async def _put_activity(body: dict, mock_firebase_user) -> MagicMock:
    """Runs update_activity on an owned activity with ``body``; returns the activities mock."""
    from app.models.Activity import ActivityUpdate
    from app.routers.annual_planning import update_activity

    existing = _owned_activity()
    acts_p, goals_p, fas_p, plans_p, pris_p = _collections()
    with acts_p as acts, goals_p as goals, fas_p as fas, plans_p as plans, pris_p:
        acts.find_one = AsyncMock(return_value=existing)
        acts.update_one = AsyncMock(return_value=_matched())
        goals.find_one = AsyncMock(return_value=_goal())
        fas.find_one = AsyncMock(return_value=_focus_area())
        plans.find_one = AsyncMock(return_value=_plan())
        await update_activity(
            id=str(existing["_id"]),
            activity=ActivityUpdate.model_validate(body),
            current_user=mock_firebase_user,
        )
    return acts


async def test_activity_put_with_title_only_leaves_the_schedule_untouched(mock_firebase_user):
    """The calendar's rename body. Nothing but the title (and the timestamp) is written."""
    acts = await _put_activity({"title": "Stretch, longer", "due_date": "2026-10-14"}, mock_firebase_user)

    written = _set_of(acts)
    assert set(written) == {"title", "updated_at"}
    assert written["title"] == "Stretch, longer"
    for field in ("frequency", "days_of_week", "time_of_day", "duration_minutes", "is_active"):
        assert field not in written


async def test_activity_put_writes_description_and_duration_when_sent(mock_firebase_user):
    """The two fields the old route silently dropped are written when the body carries them."""
    acts = await _put_activity({"description": "Hips and hamstrings", "duration_minutes": 20}, mock_firebase_user)

    written = _set_of(acts)
    assert set(written) == {"description", "duration_minutes", "updated_at"}
    assert written["duration_minutes"] == 20


async def test_activity_put_writes_the_schedule_when_sent(mock_firebase_user):
    acts = await _put_activity({"frequency": "custom", "days_of_week": [5, 6], "time_of_day": "evening"}, mock_firebase_user)

    written = _set_of(acts)
    assert written["frequency"] == "custom"
    assert written["days_of_week"] == [5, 6]
    assert written["time_of_day"] == "evening"
    assert "title" not in written


# ---------------------------------------------------------------------------
# (5) DELETE focus-areas/{id} — ownership through the plan
# ---------------------------------------------------------------------------

async def _delete_focus_area(plan_owner: str, mock_firebase_user):
    from app.routers.annual_planning import delete_focus_area

    acts_p, goals_p, fas_p, plans_p, pris_p = _collections()
    with acts_p as acts, goals_p as goals, fas_p as fas, plans_p as plans, pris_p as pris:
        fas.find_one = AsyncMock(return_value=_focus_area())
        fas.update_one = AsyncMock(return_value=_matched())
        plans.find_one = AsyncMock(return_value=_plan(plan_owner))
        for collection in (goals, acts, pris):
            collection.update_many = AsyncMock()
        result = await delete_focus_area(id=FA_ID, current_user=mock_firebase_user)
    return result, fas, goals


async def test_delete_focus_area_by_its_owner_soft_deletes_it_and_cascades(mock_firebase_user):
    result, fas, goals = await _delete_focus_area(USER_ID, mock_firebase_user)

    assert "deleted" in result["message"]
    # Looked up by id and liveness only — not by a `user_id` the document never had.
    assert "user_id" not in fas.find_one.call_args[0][0]
    filt, update = fas.update_one.call_args[0]
    assert filt == {"_id": ObjectId(FA_ID)}
    assert update["$set"]["deleted_at"] is not None
    assert update["$set"]["deleted_by"] == USER_ID
    goals.update_many.assert_awaited_once()


async def test_delete_focus_area_of_another_user_is_404(mock_firebase_user):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await _delete_focus_area(OTHER_USER_ID, mock_firebase_user)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# (5) DELETE goals/{id} — ownership through the focus area's plan
# ---------------------------------------------------------------------------

async def _delete_goal(plan_owner: str, mock_firebase_user):
    from app.routers.annual_planning import delete_goal

    acts_p, goals_p, fas_p, plans_p, pris_p = _collections()
    with acts_p as acts, goals_p as goals, fas_p as fas, plans_p as plans, pris_p:
        goals.find_one = AsyncMock(return_value=_goal())
        goals.update_one = AsyncMock(return_value=_matched())
        fas.find_one = AsyncMock(return_value=_focus_area())
        plans.find_one = AsyncMock(return_value=_plan(plan_owner))
        acts.update_many = AsyncMock()
        result = await delete_goal(id=GOAL_ID, current_user=mock_firebase_user)
    return result, goals, acts


async def test_delete_goal_by_its_owner_soft_deletes_it_and_its_activities(mock_firebase_user):
    result, goals, acts = await _delete_goal(USER_ID, mock_firebase_user)

    assert "deleted" in result["message"]
    assert "user_id" not in goals.find_one.call_args[0][0]
    filt, update = goals.update_one.call_args[0]
    assert filt == {"_id": ObjectId(GOAL_ID)}
    assert update["$set"]["deleted_at"] is not None
    assert update["$set"]["deleted_by"] == USER_ID
    acts.update_many.assert_awaited_once()


async def test_delete_goal_of_another_user_is_404(mock_firebase_user):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await _delete_goal(OTHER_USER_ID, mock_firebase_user)

    assert exc_info.value.status_code == 404
