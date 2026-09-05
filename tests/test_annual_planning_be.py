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
# BE-01 — compound sort on GET /annual-plan/priorities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_priorities_sorted(mock_firebase_user):
    """BE-01/WR-02: get_priorities aggregates with an $ifNull-normalized
    is_active sort (legacy docs missing is_active treated as active) before to_list."""
    from app.routers.annual_planning import get_priorities

    p_active = make_priority(is_completed=False, order=0)
    p_done   = make_priority(is_completed=True,  order=0)

    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[p_active, p_done])

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.aggregate = MagicMock(return_value=mock_cursor)

        result = await get_priorities(
            annual_plan_id=PLAN_ID,
            current_user=mock_firebase_user,
        )

    pipeline = mock_pri.aggregate.call_args_list[0][0][0]
    sort_stage = next(stage["$sort"] for stage in pipeline if "$sort" in stage)
    assert sort_stage == {"is_completed": 1, "_is_active_sort": -1, "order": 1, "created_at": 1}
    add_fields_stage = next(stage["$addFields"] for stage in pipeline if "$addFields" in stage)
    assert add_fields_stage == {"_is_active_sort": {"$ifNull": ["$is_active", True]}}
    assert result[0]["is_completed"] is False
    assert result[1]["is_completed"] is True


@pytest.mark.asyncio
async def test_full_priorities_sorted(mock_firebase_user):
    """BE-01/WR-02: get_full_annual_plan applies the same $ifNull-normalized
    aggregation sort to the priorities query."""
    from app.routers.annual_planning import get_full_annual_plan

    p_active = make_priority(is_completed=False, order=0)
    p_done   = make_priority(is_completed=True,  order=0)

    # priorities_coro is built with aggregate([...]).to_list() — cursor used inside asyncio.gather
    mock_pri_cursor = MagicMock()
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
        mock_pri.aggregate = MagicMock(return_value=mock_pri_cursor)
        mock_fas.find = MagicMock(return_value=_noop_cursor())
        mock_qr.find  = MagicMock(return_value=_noop_cursor())
        mock_goals.find = MagicMock(return_value=_noop_cursor())
        mock_acts.find  = MagicMock(return_value=_noop_cursor())

        response = await get_full_annual_plan(current_user=mock_firebase_user)

    pipeline = mock_pri.aggregate.call_args_list[0][0][0]
    sort_stage = next(stage["$sort"] for stage in pipeline if "$sort" in stage)
    assert sort_stage == {"is_completed": 1, "_is_active_sort": -1, "order": 1, "created_at": 1}
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
        mock_pri.aggregate = MagicMock(return_value=_noop_cursor())
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
        mock_pri.aggregate = MagicMock(return_value=_noop_cursor())
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


@pytest.mark.asyncio
async def test_reorder_oversized_payload(mock_firebase_user):
    """BE-03: reorder_priorities raises 422 when more than 50 IDs are submitted."""
    from app.routers.annual_planning import reorder_priorities, PriorityReorderRequest
    from fastapi import HTTPException

    oversized = [str(ObjectId()) for _ in range(51)]

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        with pytest.raises(HTTPException) as exc_info:
            await reorder_priorities(
                payload=PriorityReorderRequest(
                    annual_plan_id=PLAN_ID,
                    priority_ids=oversized,
                ),
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 422
    assert "50" in exc_info.value.detail


@pytest.mark.asyncio
async def test_reorder_rejects_duplicate_ids(mock_firebase_user):
    """IN-03: reorder_priorities raises 422 when priority_ids contains duplicates."""
    from app.routers.annual_planning import reorder_priorities, PriorityReorderRequest
    from fastapi import HTTPException

    dup_id = str(ObjectId())

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        with pytest.raises(HTTPException) as exc_info:
            await reorder_priorities(
                payload=PriorityReorderRequest(
                    annual_plan_id=PLAN_ID,
                    priority_ids=[dup_id, str(ObjectId()), dup_id],
                ),
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 422
    assert "duplicate" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Phase 24 — is_active partial-update (D-04 / T-24-01)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_priority_is_active(mock_firebase_user):
    """D-04: PATCH /priorities/{id} with {is_active: bool} updates is_active only, no completed_at touch."""
    from app.routers.annual_planning import update_priority

    priority_id = str(ObjectId())
    existing_doc = {
        **make_priority(is_completed=False, order=0),
        "_id": ObjectId(priority_id),
        "annual_plan_id": PLAN_ID,
    }

    mock_update_result = MagicMock()
    mock_update_result.matched_count = 1

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find_one = AsyncMock(return_value=existing_doc)
        mock_pri.update_one = AsyncMock(return_value=mock_update_result)

        await update_priority(
            id=priority_id,
            priority_update={"is_active": False},
            current_user=mock_firebase_user,
        )

    set_payload = mock_pri.update_one.call_args_list[0][0][1]["$set"]
    assert set_payload["is_active"] is False
    assert "completed_at" not in set_payload


@pytest.mark.asyncio
async def test_patch_priority_is_active_rejects_non_bool(mock_firebase_user):
    """T-24-01/WR-01: non-bool is_active input raises 400 instead of being
    silently coerced — bool("false")/bool(0)-style casts would invert a
    caller's intent for stringified falsy values without ever raising."""
    from app.routers.annual_planning import update_priority
    from fastapi import HTTPException

    priority_id = str(ObjectId())
    existing_doc = {
        **make_priority(is_completed=False, order=0),
        "_id": ObjectId(priority_id),
        "annual_plan_id": PLAN_ID,
    }

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find_one = AsyncMock(return_value=existing_doc)
        mock_pri.update_one = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await update_priority(
                id=priority_id,
                priority_update={"is_active": "false"},
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 400
    mock_pri.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_patch_priority_is_active_accepts_real_bool(mock_firebase_user):
    """WR-01: a genuine JSON boolean (the only shape the shipped React client
    sends) still updates is_active as before."""
    from app.routers.annual_planning import update_priority

    priority_id = str(ObjectId())
    existing_doc = {
        **make_priority(is_completed=False, order=0),
        "_id": ObjectId(priority_id),
        "annual_plan_id": PLAN_ID,
    }

    mock_update_result = MagicMock()
    mock_update_result.matched_count = 1

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find_one = AsyncMock(return_value=existing_doc)
        mock_pri.update_one = AsyncMock(return_value=mock_update_result)

        await update_priority(
            id=priority_id,
            priority_update={"is_active": True},
            current_user=mock_firebase_user,
        )

    set_payload = mock_pri.update_one.call_args_list[0][0][1]["$set"]
    assert set_payload["is_active"] is True
    assert isinstance(set_payload["is_active"], bool)


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


@pytest.mark.asyncio
async def test_update_priority_reparent_requires_target_plan_ownership(mock_firebase_user):
    """CR-02: reassigning a priority's annual_plan_id must verify the caller
    owns the *target* plan, not just the plan the priority currently belongs to."""
    from app.routers.annual_planning import update_priority
    from fastapi import HTTPException

    priority_id = str(ObjectId())
    existing_doc = {
        **make_priority(is_completed=False, order=0),
        "_id": ObjectId(priority_id),
        "annual_plan_id": PLAN_ID,
    }
    other_users_plan_id = "507f1f77bcf86cd799439099"
    other_users_plan = {**make_plan(), "_id": ObjectId(other_users_plan_id), "user_id": "someone-else"}

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri:
        # verify_priority_ownership (current plan) succeeds; the reparent target does not.
        mock_plans.find_one = AsyncMock(side_effect=[make_plan(), other_users_plan, other_users_plan])
        mock_pri.find_one = AsyncMock(return_value=existing_doc)
        mock_pri.update_one = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await update_priority(
                id=priority_id,
                priority_update={"annual_plan_id": other_users_plan_id},
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 403
    mock_pri.update_one.assert_not_called()


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


# ---------------------------------------------------------------------------
# ADR-015 / PRIO-002 — completing a task-linked priority carries its task
# ---------------------------------------------------------------------------

def _fake_agent_module():
    """Stand-in for app.routers.agent so the lazy XP import never loads the
    real module (which drags in the LLM client)."""
    agent = MagicMock()
    agent.grant_xp = AsyncMock(return_value={})
    agent.XP_PER_TASK_COMPLETE = 10
    return agent


def _task_linked_priority(priority_id: str, task_id: str):
    return {
        **make_priority(is_completed=False, order=0),
        "_id": ObjectId(priority_id),
        "annual_plan_id": PLAN_ID,
        "linked_entity_type": "task",
        "linked_entity_id": task_id,
    }


def _matched():
    result = MagicMock()
    result.matched_count = 1
    return result


async def _complete_priority(existing_doc, payload, task_doc, mock_firebase_user):
    """Runs update_priority with every collection mocked; returns (tasks mock, agent stub)."""
    from app.routers.annual_planning import update_priority

    agent = _fake_agent_module()
    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri, \
         patch("app.routers.annual_planning.tasks_collection") as mock_tasks, \
         patch.dict(sys.modules, {"app.routers.agent": agent}):
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find_one = AsyncMock(return_value=existing_doc)
        mock_pri.update_one = AsyncMock(return_value=_matched())
        mock_tasks.find_one = AsyncMock(return_value=task_doc)
        mock_tasks.update_one = AsyncMock(return_value=_matched())

        await update_priority(
            id=str(existing_doc["_id"]),
            priority_update=payload,
            current_user=mock_firebase_user,
        )
    return mock_tasks, agent


@pytest.mark.asyncio
async def test_complete_task_linked_priority_completes_task_and_awards_xp(mock_firebase_user):
    """ADR-015 point 4: the task follows the priority, and the task's XP is
    awarded once because the task was not complete before."""
    task_id = str(ObjectId())
    existing = _task_linked_priority(str(ObjectId()), task_id)
    task_doc = {"_id": ObjectId(task_id), "user_id": USER_ID, "is_completed": False, "deleted_at": None}

    mock_tasks, agent = await _complete_priority(existing, {"is_completed": True}, task_doc, mock_firebase_user)

    mock_tasks.find_one.assert_awaited_once()
    task_set = mock_tasks.update_one.call_args[0][1]["$set"]
    assert task_set["is_completed"] is True
    agent.grant_xp.assert_awaited_once_with(USER_ID, 10)


@pytest.mark.asyncio
async def test_complete_task_linked_priority_is_noop_when_task_already_done(mock_firebase_user):
    """Idempotent: a task that is already complete is neither rewritten nor re-awarded."""
    task_id = str(ObjectId())
    existing = _task_linked_priority(str(ObjectId()), task_id)
    task_doc = {"_id": ObjectId(task_id), "user_id": USER_ID, "is_completed": True, "deleted_at": None}

    mock_tasks, agent = await _complete_priority(existing, {"is_completed": True}, task_doc, mock_firebase_user)

    mock_tasks.update_one.assert_not_called()
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_uncomplete_task_linked_priority_reverts_task_without_xp(mock_firebase_user):
    task_id = str(ObjectId())
    existing = {**_task_linked_priority(str(ObjectId()), task_id), "is_completed": True}
    task_doc = {"_id": ObjectId(task_id), "user_id": USER_ID, "is_completed": True, "deleted_at": None}

    mock_tasks, agent = await _complete_priority(existing, {"is_completed": False}, task_doc, mock_firebase_user)

    task_set = mock_tasks.update_one.call_args[0][1]["$set"]
    assert task_set["is_completed"] is False
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_complete_priority_skips_task_owned_by_someone_else(mock_firebase_user):
    """Ownership is transitive through the priority, but a linked id that points
    at another user's task must still never be written."""
    task_id = str(ObjectId())
    existing = _task_linked_priority(str(ObjectId()), task_id)
    task_doc = {"_id": ObjectId(task_id), "user_id": "someone-else", "is_completed": False, "deleted_at": None}

    mock_tasks, agent = await _complete_priority(existing, {"is_completed": True}, task_doc, mock_firebase_user)

    mock_tasks.update_one.assert_not_called()
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_complete_priority_with_missing_task_is_silent(mock_firebase_user):
    task_id = str(ObjectId())
    existing = _task_linked_priority(str(ObjectId()), task_id)

    mock_tasks, agent = await _complete_priority(existing, {"is_completed": True}, None, mock_firebase_user)

    mock_tasks.update_one.assert_not_called()
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_complete_goal_linked_priority_never_touches_tasks(mock_firebase_user):
    """A goal-linked priority completes on its own: no task lookup, no goal write (point 4)."""
    existing = {
        **make_priority(is_completed=False, order=0),
        "annual_plan_id": PLAN_ID,
        "linked_entity_type": "goal",
        "linked_entity_id": str(ObjectId()),
    }

    mock_tasks, agent = await _complete_priority(existing, {"is_completed": True}, None, mock_firebase_user)

    mock_tasks.find_one.assert_not_called()
    mock_tasks.update_one.assert_not_called()
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_patch_priority_is_completed_rejects_non_bool(mock_firebase_user):
    """The same guard is_active has: a stringified boolean is a 400, never a coerced write."""
    from app.routers.annual_planning import update_priority
    from fastapi import HTTPException

    existing = _task_linked_priority(str(ObjectId()), str(ObjectId()))

    with patch("app.routers.annual_planning.annual_plans_collection") as mock_plans, \
         patch("app.routers.annual_planning.priorities_collection") as mock_pri, \
         patch("app.routers.annual_planning.tasks_collection") as mock_tasks:
        mock_plans.find_one = AsyncMock(return_value=make_plan())
        mock_pri.find_one = AsyncMock(return_value=existing)
        mock_pri.update_one = AsyncMock()
        mock_tasks.find_one = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await update_priority(
                id=str(existing["_id"]),
                priority_update={"is_completed": "true"},
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 400
    mock_pri.update_one.assert_not_called()
    mock_tasks.find_one.assert_not_called()
