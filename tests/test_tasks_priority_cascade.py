"""
PATCH /tasks/{id} completion side effects.

ADR-015 point 4 used to carry every yearly priority linked to a task along
with its completion. Since PLAN-013 (ADR-046) a priority *is* the task's own
`is_priority` flag, so there is nothing to carry: the route touches no
priority collection, and only the XP award remains a side effect.

The handler is called directly with its dependencies supplied as mocks, the
way test_annual_planning_be.py drives the annual-planning router.
"""
from __future__ import annotations

import sys

from tests._stubs import use_stub_if_missing
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from bson import ObjectId

_mock_firebase = MagicMock()
_mock_firebase.get_firebase_user = MagicMock()
use_stub_if_missing("app.auth.firebase_auth", _mock_firebase)

_mock_db = MagicMock()
sys.modules.setdefault("app.config.database", _mock_db)

USER_ID = "507f1f77bcf86cd799439011"


def _fake_agent_module():
    agent = MagicMock()
    agent.grant_xp = AsyncMock(return_value={})
    agent.XP_PER_TASK_COMPLETE = 10
    return agent


async def _patch_task(task_doc, updates, mock_firebase_user):
    """Returns (tasks collection mock, agent stub)."""
    from app.routers.tasks import update_task

    task_id = str(task_doc["_id"])
    collection = MagicMock()
    collection.update_one = AsyncMock()
    collection.find_one = AsyncMock(return_value={**task_doc, **updates})
    agent = _fake_agent_module()

    with patch.dict(sys.modules, {"app.routers.agent": agent}):
        await update_task(
            id=task_id,
            updates=dict(updates),
            collection=collection,
            task=task_doc,
            user=mock_firebase_user,
        )
    return collection, agent


def _task(is_completed: bool):
    return {"_id": ObjectId(), "user_id": USER_ID, "title": "T", "is_completed": is_completed, "deleted_at": None}


def test_the_tasks_router_no_longer_knows_the_priorities_collection():
    """ADR-046: the flag lives on the task; no cross-collection sync remains."""
    from app.routers import tasks as tasks_router

    assert not hasattr(tasks_router, "priorities_collection")
    assert not hasattr(tasks_router, "sync_linked_priorities_completion")


@pytest.mark.asyncio
async def test_completing_task_writes_it_and_awards_xp_once(mock_firebase_user):
    task_doc = _task(is_completed=False)

    collection, agent = await _patch_task(task_doc, {"is_completed": True}, mock_firebase_user)

    assert collection.update_one.call_args[0][1]["$set"]["is_completed"] is True
    agent.grant_xp.assert_awaited_once_with(USER_ID, 10)


@pytest.mark.asyncio
async def test_uncompleting_task_awards_nothing(mock_firebase_user):
    task_doc = _task(is_completed=True)

    collection, agent = await _patch_task(task_doc, {"is_completed": False}, mock_firebase_user)

    assert collection.update_one.call_args[0][1]["$set"]["is_completed"] is False
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_task_patch_without_state_change_awards_nothing(mock_firebase_user):
    task_doc = _task(is_completed=True)

    _, agent = await _patch_task(task_doc, {"is_completed": True}, mock_firebase_user)

    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_task_patch_of_other_fields_awards_nothing(mock_firebase_user):
    task_doc = _task(is_completed=False)

    collection, agent = await _patch_task(task_doc, {"title": "Renamed"}, mock_firebase_user)

    assert collection.update_one.call_args[0][1]["$set"]["title"] == "Renamed"
    agent.grant_xp.assert_not_called()
