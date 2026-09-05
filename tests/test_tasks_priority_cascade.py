"""
ADR-015 / PRIO-002 — completing a task carries every priority linked to it.

PATCH /tasks/{id} is called directly with its dependencies supplied as mocks,
the way test_annual_planning_be.py drives the annual-planning router.
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
    """Returns (priorities mock, tasks collection mock, agent stub)."""
    from app.routers.tasks import update_task

    task_id = str(task_doc["_id"])
    collection = MagicMock()
    collection.update_one = AsyncMock()
    collection.find_one = AsyncMock(return_value={**task_doc, **updates})
    agent = _fake_agent_module()

    with patch("app.routers.tasks.priorities_collection") as mock_pri, \
         patch.dict(sys.modules, {"app.routers.agent": agent}):
        mock_pri.update_many = AsyncMock()
        await update_task(
            id=task_id,
            updates=dict(updates),
            collection=collection,
            task=task_doc,
            user=mock_firebase_user,
        )
    return mock_pri, collection, agent


def _task(is_completed: bool):
    return {"_id": ObjectId(), "user_id": USER_ID, "title": "T", "is_completed": is_completed, "deleted_at": None}


@pytest.mark.asyncio
async def test_completing_task_completes_linked_priorities(mock_firebase_user):
    task_doc = _task(is_completed=False)

    mock_pri, _, agent = await _patch_task(task_doc, {"is_completed": True}, mock_firebase_user)

    mock_pri.update_many.assert_awaited_once()
    query, update = mock_pri.update_many.call_args[0]
    assert query == {"linked_entity_id": str(task_doc["_id"]), "linked_entity_type": "task"}
    assert update["$set"]["is_completed"] is True
    assert update["$set"]["completed_at"] is not None
    agent.grant_xp.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncompleting_task_reverts_linked_priorities(mock_firebase_user):
    task_doc = _task(is_completed=True)

    mock_pri, _, agent = await _patch_task(task_doc, {"is_completed": False}, mock_firebase_user)

    _, update = mock_pri.update_many.call_args[0]
    assert update["$set"]["is_completed"] is False
    assert update["$set"]["completed_at"] is None
    agent.grant_xp.assert_not_called()


@pytest.mark.asyncio
async def test_task_patch_without_state_change_touches_no_priority(mock_firebase_user):
    task_doc = _task(is_completed=True)

    mock_pri, _, _ = await _patch_task(task_doc, {"is_completed": True}, mock_firebase_user)

    mock_pri.update_many.assert_not_called()


@pytest.mark.asyncio
async def test_task_patch_of_other_fields_touches_no_priority(mock_firebase_user):
    task_doc = _task(is_completed=False)

    mock_pri, _, _ = await _patch_task(task_doc, {"title": "Renamed"}, mock_firebase_user)

    mock_pri.update_many.assert_not_called()
