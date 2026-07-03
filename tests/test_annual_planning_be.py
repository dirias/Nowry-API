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

@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 1 (BE-01)")
@pytest.mark.asyncio
async def test_get_priorities_sorted(mock_firebase_user):
    """BE-01: get_priorities chains .sort([('is_completed',1),('order',1),('created_at',1)]) on the cursor."""
    pass


@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 1 (BE-01)")
@pytest.mark.asyncio
async def test_full_priorities_sorted(mock_firebase_user):
    """BE-01: get_full_annual_plan applies the same compound sort to the priorities_coro query."""
    pass


# ---------------------------------------------------------------------------
# BE-02 — _computed_progress in GET /annual-plan/full goals
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 2 (BE-02)")
@pytest.mark.asyncio
async def test_computed_progress_present(mock_firebase_user):
    """BE-02: every goal dict in the /full response contains a _computed_progress key."""
    pass


@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 2 (BE-02)")
@pytest.mark.asyncio
async def test_computed_progress_value(mock_firebase_user):
    """BE-02: _computed_progress equals goal['progress']; missing progress field defaults to 0."""
    pass


# ---------------------------------------------------------------------------
# BE-03 — PATCH /annual-plan/priorities/reorder
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 3 (BE-03)")
@pytest.mark.asyncio
async def test_reorder_success(mock_firebase_user):
    """BE-03: reorder_priorities returns {'ok': True} when all IDs belong to the plan."""
    pass


@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 3 (BE-03)")
@pytest.mark.asyncio
async def test_reorder_invalid_ids(mock_firebase_user):
    """BE-03: reorder_priorities raises HTTPException 422 if count_documents returns fewer than len(priority_ids)."""
    pass


@pytest.mark.skip(reason="Implementing in 20-01-PLAN Task 3 (BE-03)")
@pytest.mark.asyncio
async def test_reorder_auth_required(mock_firebase_user):
    """BE-03: reorder_priorities raises HTTPException 403 when plan is not owned by calling user."""
    pass
