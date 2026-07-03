"""
Phase 20 — Backend Data Layer tests.
BE-01: compound sort on GET /annual-plan/priorities and GET /annual-plan/full
BE-02: _computed_progress field injected into every goal in GET /annual-plan/full
BE-03: PATCH /annual-plan/priorities/reorder endpoint

Wave 0: stubs only — all tests skipped. Remove @pytest.mark.skip in Tasks 1-3
as each requirement is implemented.
"""
from __future__ import annotations

import sys
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
sys.modules.setdefault("app.auth.firebase_auth", _mock_firebase)

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


def make_priority(is_completed: bool, order: int):
    return {
        "_id": ObjectId(),
        "annual_plan_id": PLAN_ID,
        "title": f"Priority order={order} completed={is_completed}",
        "is_completed": is_completed,
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
# BE-01 — compound sort on GET /annual-plan/priorities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_priorities_sorted(mock_firebase_user):
    """BE-01: get_priorities chains sort([('is_completed',1),('order',1),('created_at',1)]) before to_list."""
    from app.routers.annual_planning import get_priorities

    p_active = make_priority(is_completed=False, order=0)
    p_done   = make_priority(is_completed=True,  order=0)

    mock_cursor = MagicMock()
    mock_cursor.sort = MagicMock(return_value=mock_cursor)
    mock_cursor.to_list = AsyncMock(return_value=[p_active, p_done])

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find = MagicMock(return_value=mock_cursor)

        result = await get_priorities(
            annual_plan_id=PLAN_ID,
            current_user=mock_firebase_user,
        )

    mock_cursor.sort.assert_called_once_with(
        [("is_completed", 1), ("order", 1), ("created_at", 1)]
    )
    assert result[0]["is_completed"] is False
    assert result[1]["is_completed"] is True


@pytest.mark.asyncio
async def test_full_priorities_sorted(mock_firebase_user):
    """BE-01: get_full_annual_plan applies the same compound sort to the priorities query."""
    from app.routers.annual_planning import get_full_annual_plan

    p_active = make_priority(is_completed=False, order=0)
    p_done   = make_priority(is_completed=True,  order=0)

    # priorities_coro is built with find().sort([...]).to_list() — cursor used inside asyncio.gather
    mock_pri_cursor = MagicMock()
    mock_pri_cursor.sort = MagicMock(return_value=mock_pri_cursor)
    mock_pri_cursor.to_list = AsyncMock(return_value=[p_active, p_done])

    # Minimal cursor stubs for the other coroutines in the gather
    def _noop_cursor():
        c = MagicMock()
        c.sort = MagicMock(return_value=c)
        c.to_list = AsyncMock(return_value=[])
        return c

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri, \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.activities_collection") as mock_acts:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find = MagicMock(return_value=mock_pri_cursor)
        mock_fas.find = MagicMock(return_value=_noop_cursor())
        mock_qr.find  = MagicMock(return_value=_noop_cursor())
        mock_goals.find = MagicMock(return_value=_noop_cursor())
        mock_acts.find  = MagicMock(return_value=_noop_cursor())

        response = await get_full_annual_plan(current_user=mock_firebase_user)

    mock_pri_cursor.sort.assert_called_once_with(
        [("is_completed", 1), ("order", 1), ("created_at", 1)]
    )
    priorities = response["priorities"] if isinstance(response, dict) else response.priorities
    assert priorities[0]["is_completed"] is False
    assert priorities[1]["is_completed"] is True


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
         patch("app.routers.annual_planning.priorities_collection") as mock_pri, \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.activities_collection") as mock_acts:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find  = MagicMock(return_value=_noop_cursor())
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
         patch("app.routers.annual_planning.priorities_collection") as mock_pri, \
         patch("app.routers.annual_planning.focus_areas_collection") as mock_fas, \
         patch("app.routers.annual_planning.quarter_reports_collection") as mock_qr, \
         patch("app.routers.annual_planning.goals_collection") as mock_goals, \
         patch("app.routers.annual_planning.activities_collection") as mock_acts:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find  = MagicMock(return_value=_noop_cursor())
        mock_fas.find  = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[mock_area])))
        mock_qr.find   = MagicMock(return_value=_noop_cursor())
        mock_goals.find = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[goal_42, goal_none])))
        mock_acts.find  = MagicMock(return_value=_noop_cursor())

        response = await get_full_annual_plan(current_user=mock_firebase_user)

    goals = response["goals"] if isinstance(response, dict) else response.goals
    assert goals[0]["_computed_progress"] == 42
    assert goals[1]["_computed_progress"] == 0


# ---------------------------------------------------------------------------
# BE-03 — PATCH /annual-plan/priorities/reorder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reorder_success(mock_firebase_user):
    """BE-03: reorder_priorities returns {'ok': True} when all IDs belong to the plan."""
    from app.routers.annual_planning import reorder_priorities, PriorityReorderRequest
    from fastapi import HTTPException

    pri_ids = [str(ObjectId()), str(ObjectId())]

    mock_update_result = MagicMock()
    mock_update_result.matched_count = 1

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.count_documents = AsyncMock(return_value=len(pri_ids))
        mock_pri.update_one = AsyncMock(return_value=mock_update_result)

        result = await reorder_priorities(
            payload=PriorityReorderRequest(
                annual_plan_id=PLAN_ID,
                priority_ids=pri_ids,
            ),
            current_user=mock_firebase_user,
        )

    assert result == {"ok": True} or getattr(result, "ok", None) is True
    assert mock_pri.update_one.call_count == len(pri_ids)
    # Verify order values: first call sets order=0, second sets order=1
    first_set  = mock_pri.update_one.call_args_list[0][0][1]["$set"]
    second_set = mock_pri.update_one.call_args_list[1][0][1]["$set"]
    assert first_set["order"] == 0
    assert second_set["order"] == 1


@pytest.mark.asyncio
async def test_reorder_invalid_ids(mock_firebase_user):
    """BE-03: reorder_priorities raises 422 when count_documents < len(priority_ids)."""
    from app.routers.annual_planning import reorder_priorities, PriorityReorderRequest
    from fastapi import HTTPException

    pri_ids = [str(ObjectId()), str(ObjectId()), "nonexistent-id"]

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        # Only 2 of the 3 IDs are valid (simulates one missing/wrong-plan ID)
        mock_pri.count_documents = AsyncMock(return_value=2)

        with pytest.raises(HTTPException) as exc_info:
            await reorder_priorities(
                payload=PriorityReorderRequest(
                    annual_plan_id=PLAN_ID,
                    priority_ids=pri_ids,
                ),
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 422
    assert "do not belong to this plan" in exc_info.value.detail


@pytest.mark.asyncio
async def test_reorder_auth_required(mock_firebase_user):
    """BE-03: reorder_priorities raises 403 when plan is not owned by calling user."""
    from app.routers.annual_planning import reorder_priorities, PriorityReorderRequest
    from fastapi import HTTPException

    pri_ids = [str(ObjectId())]

    # Plan belongs to a different user — verify_annual_plan_ownership raises 403
    wrong_plan = {**make_plan(), "user_id": "000000000000000000000000"}

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans:
        mock_plans.find_one = AsyncMock(return_value=wrong_plan)

        with pytest.raises(HTTPException) as exc_info:
            await reorder_priorities(
                payload=PriorityReorderRequest(
                    annual_plan_id=PLAN_ID,
                    priority_ids=pri_ids,
                ),
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 403
