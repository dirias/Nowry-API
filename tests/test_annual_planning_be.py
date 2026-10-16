"""
Phase 20 — Backend Data Layer tests.
BE-01: GET /annual-plan/priorities and GET /annual-plan/full answer the flagged
       tasks and milestones (PLAN-013, ADR-046) — the old compound sort on the
       priorities collection is retired with the collection's readers.
BE-02: _computed_progress field injected into every goal in GET /annual-plan/full
BE-03: the priority writers (POST/PUT/DELETE, PATCH reorder) answer 410 Gone.
"""
from __future__ import annotations

import sys

from tests._stubs import use_stub_if_missing
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from bson import ObjectId
from datetime import datetime

# ---------------------------------------------------------------------------
# Module-level sys.modules stubs — installed before any app import.
# Mirrors the pattern in test_sheets.py.
# ---------------------------------------------------------------------------
_mock_firebase = MagicMock()
_mock_firebase.get_firebase_user = MagicMock()
use_stub_if_missing("app.auth.firebase_auth", _mock_firebase)

_mock_db = MagicMock()
sys.modules.setdefault("app.config.database", _mock_db)

# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------
PLAN_ID = "507f1f77bcf86cd799439001"
USER_ID  = "507f1f77bcf86cd799439011"


def make_plan():
    return {
        "_id": ObjectId(PLAN_ID),
        "user_id": USER_ID,
        "year": 2026,
        "deleted_at": None,
    }


def make_priority(is_completed: bool, order: int, is_active: bool = True):
    return {
        "_id": ObjectId(),
        "annual_plan_id": PLAN_ID,
        "title": f"Priority order={order} completed={is_completed}",
        "is_completed": is_completed,
        "is_active": is_active,
        "order": order,
        "created_at": datetime(2026, 1, 1),
        "deleted_at": None,
    }


def make_goal(progress: int = 42, include_progress: bool = True):
    doc = {
        "_id": ObjectId(),
        "focus_area_id": "area1",
        "title": "Test Goal",
        "deleted_at": None,
    }
    if include_progress:
        doc["progress"] = progress
    return doc


# ---------------------------------------------------------------------------
# Phase 24 — is_active field (D-01/D-02/D-03)
# ---------------------------------------------------------------------------

def test_priority_model_is_active_default():
    """D-01/D-02: is_active defaults to True when not provided (lazy-bootstrap, no migration)."""
    from app.models.Priority import Priority

    p = Priority(annual_plan_id="plan1", title="Test Priority")
    assert p.is_active is True


# ---------------------------------------------------------------------------
# BE-01 — the flagged list on GET /annual-plan/priorities and /full (PLAN-013)
# ---------------------------------------------------------------------------

def _flagged_rows():
    """A flagged task and a flagged milestone, as flagged_items projects them."""
    from app.services.priorities import milestone_as_priority, task_as_priority

    task = {"_id": ObjectId(), "user_id": USER_ID, "title": "Essay", "is_completed": False,
            "deadline": datetime(2026, 10, 20), "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1)}
    goal = {"_id": ObjectId(), "focus_area_id": "area1", "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1)}
    step = {"id": str(ObjectId()), "title": "Chapter 2", "due_date": None, "completed": True, "is_priority": True}
    return [task_as_priority(task, PLAN_ID), milestone_as_priority(step, goal, PLAN_ID)]


@pytest.mark.asyncio
async def test_get_priorities_answers_flagged_items(mock_firebase_user):
    """BE-01: get_priorities verifies the plan and answers flagged_items(user, plan)."""
    from app.routers.annual_planning import get_priorities

    rows = _flagged_rows()
    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.flagged_items", AsyncMock(return_value=rows)) as flagged:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        result = await get_priorities(annual_plan_id=PLAN_ID, current_user=mock_firebase_user)

    flagged.assert_awaited_once_with(USER_ID, PLAN_ID)
    assert [r["linked_entity_type"] for r in result] == ["task", "milestone"]
    assert result[0]["is_completed"] is False and result[1]["is_completed"] is True


@pytest.mark.asyncio
async def test_full_priorities_are_the_flagged_items(mock_firebase_user):
    """BE-01: get_full_annual_plan's `priorities` is the same flagged list, serialized."""
    from app.routers.annual_planning import get_full_annual_plan

    rows = _flagged_rows()

    def _noop_cursor():
        c = MagicMock()
        c.sort = MagicMock(return_value=c)
        c.to_list = AsyncMock(return_value=[])
        return c

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.flagged_items", AsyncMock(return_value=rows)) as flagged, \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.activities_collection") as mock_acts:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_fas.find = MagicMock(return_value=_noop_cursor())
        mock_qr.find  = MagicMock(return_value=_noop_cursor())
        mock_goals.find = MagicMock(return_value=_noop_cursor())
        mock_acts.find  = MagicMock(return_value=_noop_cursor())

        response = await get_full_annual_plan(current_user=mock_firebase_user)

    flagged.assert_awaited_once_with(USER_ID, PLAN_ID)
    priorities = response["priorities"] if isinstance(response, dict) else response.priorities
    assert priorities[0]["_id"] == str(rows[0]["_id"]) and priorities[0]["linked_entity_type"] == "task"
    assert priorities[1]["_id"] == rows[1]["_id"] and priorities[1]["linked_entity_type"] == "milestone"


# ---------------------------------------------------------------------------
# BE-02 — _computed_progress in GET /annual-plan/full goals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_computed_progress_present(mock_firebase_user):
    """BE-02: every goal dict in the /full response has a _computed_progress key."""
    from app.routers.annual_planning import get_full_annual_plan

    goal_with_progress = make_goal(progress=50)
    goal_no_progress   = make_goal(include_progress=False)

    def _noop_cursor():
        c = MagicMock()
        c.sort = MagicMock(return_value=c)
        c.to_list = AsyncMock(return_value=[])
        return c

    mock_goal_cursor = MagicMock()
    mock_goal_cursor.to_list = AsyncMock(return_value=[goal_with_progress, goal_no_progress])

    mock_area = {"_id": ObjectId(), "annual_plan_id": PLAN_ID, "deleted_at": None}

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.flagged_items", AsyncMock(return_value=[])), \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.activities_collection") as mock_acts:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_fas.find  = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[mock_area])))
        mock_qr.find   = MagicMock(return_value=_noop_cursor())
        mock_goals.find = MagicMock(return_value=mock_goal_cursor)
        mock_acts.find  = MagicMock(return_value=_noop_cursor())

        response = await get_full_annual_plan(current_user=mock_firebase_user)

    goals = response["goals"] if isinstance(response, dict) else response.goals
    assert len(goals) == 2
    assert all("_computed_progress" in g for g in goals)


@pytest.mark.asyncio
async def test_computed_progress_value(mock_firebase_user):
    """BE-02: _computed_progress equals goal['progress']; absent progress field defaults to 0."""
    from app.routers.annual_planning import get_full_annual_plan

    goal_42  = make_goal(progress=42)
    goal_none = make_goal(include_progress=False)

    def _noop_cursor():
        c = MagicMock()
        c.sort = MagicMock(return_value=c)
        c.to_list = AsyncMock(return_value=[])
        return c

    mock_area = {"_id": ObjectId(), "annual_plan_id": PLAN_ID, "deleted_at": None}

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.flagged_items", AsyncMock(return_value=[])), \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.activities_collection") as mock_acts:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_fas.find  = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[mock_area])))
        mock_qr.find   = MagicMock(return_value=_noop_cursor())
        mock_goals.find = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[goal_42, goal_none])))
        mock_acts.find  = MagicMock(return_value=_noop_cursor())

        response = await get_full_annual_plan(current_user=mock_firebase_user)

    goals = response["goals"] if isinstance(response, dict) else response.goals
    assert goals[0]["_computed_progress"] == 42
    assert goals[1]["_computed_progress"] == 0


# ---------------------------------------------------------------------------
# BE-03 — the priority writers answer 410 Gone (PLAN-013, ADR-046 §5)
# ---------------------------------------------------------------------------

GONE = "Priorities are a flag on tasks and milestones (ADR-046); write is_priority on the item"


@pytest.mark.asyncio
async def test_reorder_is_gone(mock_firebase_user):
    """BE-03: the reorder route stays registered and answers 410 with the reason."""
    from app.routers.annual_planning import reorder_priorities
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await reorder_priorities(current_user=mock_firebase_user)

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == GONE


@pytest.mark.asyncio
async def test_create_update_delete_priority_are_gone(mock_firebase_user):
    """The writers never touch a collection: no plan lookup, no priority write."""
    from app.routers.annual_planning import create_priority, delete_priority, update_priority
    from fastapi import HTTPException

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock()
        mock_pri.update_one = AsyncMock()
        mock_pri.insert_one = AsyncMock()
        for call in (
            create_priority(current_user=mock_firebase_user),
            update_priority(id=str(ObjectId()), current_user=mock_firebase_user),
            delete_priority(id=str(ObjectId()), current_user=mock_firebase_user),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await call
            assert exc_info.value.status_code == 410
            assert exc_info.value.detail == GONE

    mock_plans.find_one.assert_not_called()
    mock_pri.update_one.assert_not_called()
    mock_pri.insert_one.assert_not_called()


def test_the_retired_helpers_are_gone():
    """PriorityReorderRequest, verify_priority_ownership, sync_linked_task_completion
    and the sort pipeline left with the writers."""
    from app.routers import annual_planning as router

    for name in ("PriorityReorderRequest", "verify_priority_ownership", "sync_linked_task_completion", "_priorities_sort_pipeline"):
        assert not hasattr(router, name), name


# ---------------------------------------------------------------------------
# Code review fixes (24-REVIEW.md) — CR-01, CR-02
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_goal_invalid_id_returns_404_not_unboundlocalerror(mock_firebase_user):
    """CR-01: a goal id that ObjectId() can't parse used to crash with
    UnboundLocalError (update_data was only assigned inside the try block).
    It must now resolve cleanly to a 404 via the string-_id fallback."""
    from app.routers.annual_planning import update_goal
    from fastapi import HTTPException

    bad_id = "not-a-valid-object-id"
    owned_goal = {"_id": bad_id, "focus_area_id": "fa-1"}

    with patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.annual_plans_collection") as mock_plans:
        # verify_goal_ownership -> verify_focus_area_ownership -> verify_annual_plan_ownership
        mock_goals.find_one = AsyncMock(return_value=owned_goal)
        mock_fas.find_one = AsyncMock(return_value={"_id": "fa-1", "annual_plan_id": PLAN_ID})
        mock_plans.find_one = AsyncMock(return_value=make_plan())

        mock_update_result = MagicMock()
        mock_update_result.matched_count = 0
        mock_goals.update_one = AsyncMock(return_value=mock_update_result)

        with pytest.raises(HTTPException) as exc_info:
            await update_goal(
                id=bad_id,
                goal_update={"title": "New title"},
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Phase 26 — D-06: close_quarter routines_summary is key-format-agnostic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_quarter_routine_count(mock_firebase_user):
    """D-06: close_quarter's routines_summary counts daily_completions by
    array length only (len(items) > 0) — id-based keys (Phase 26's new
    format) produce identical counts to the legacy index-based keys they
    replace. Proves no backend change is required for the Phase 26 frontend
    key-format migration (D-03/D-04)."""
    from app.routers.annual_planning import close_quarter

    def _cursor(items=None):
        c = MagicMock()
        c.to_list = AsyncMock(return_value=items or [])
        return c

    # Id-based (UUID) keys — the exact format DailyRoutinePlanner.js/SideMenu.js
    # write after Phase 26's migration. One in-quarter date has 2 completed
    # items, one in-quarter date has zero, one date falls OUTSIDE Q1 2026.
    routine_doc = {
        "user_id": USER_ID,
        "daily_completions": {
            "2026-01-15": [
                "550e8400-e29b-41d4-a716-446655440000",
                "660e8400-e29b-41d4-a716-446655440001",
            ],
            "2026-02-01": [],
            "2025-12-31": ["should-not-count-outside-quarter"],
        },
    }

    mock_insert_result = MagicMock()
    mock_insert_result.inserted_id = ObjectId()

    with patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.books_collection") as mock_books, \
         patch("app.routers.annual_planning.daily_routines_collection") as mock_routines, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr:
        mock_fas.find = MagicMock(return_value=_cursor([]))
        mock_goals.find = MagicMock(return_value=_cursor([]))
        mock_books.find = MagicMock(return_value=_cursor([]))
        mock_routines.find_one = AsyncMock(return_value=routine_doc)
        mock_qr.insert_one = AsyncMock(return_value=mock_insert_result)
        mock_qr.find_one = AsyncMock(return_value={"_id": mock_insert_result.inserted_id})

        await close_quarter(
            payload={"year": 2026, "quarter": 1, "annual_plan_id": PLAN_ID},
            current_user=mock_firebase_user,
        )

    saved_doc = mock_qr.insert_one.call_args[0][0]
    assert saved_doc["routines_summary"]["active_days"] == 1
    assert saved_doc["routines_summary"]["total_items_checked"] == 2
