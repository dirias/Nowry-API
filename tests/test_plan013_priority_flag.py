"""PLAN-013 — priority is a flag (ADR-046 §5, PRD D6 / FR-001).

Pinned here:

  1. `is_priority` defaults to False on a task and on a milestone.
  2. `flagged_items` projects flagged tasks and milestones into the old Priority
     shape (the response model accepts every row), orders open before done and
     dated before undated, and caps at 50.
  3. GET /annual-plan/priorities answers that list; POST, PUT, DELETE and the
     reorder PATCH answer 410 with the ADR-046 reason.
  4. The milestone routes carry the flag; PATCH /tasks refuses a non-boolean.
  5. The migration's builders map a task-linked, a goal-linked, a routine-linked
     and an unlinked priority as the script's docstring says; a migrated
     priority is skipped; a dry run writes nothing.

Handlers are called directly with the `Depends()` parameters as kwargs and the
collections patched, as test_blocks.py does.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

from app.models.Goal import Milestone  # noqa: E402
from app.models.Priority import Priority  # noqa: E402
from app.models.Task import Task  # noqa: E402
from app.routers import annual_planning as router  # noqa: E402
from app.routers import tasks as tasks_router  # noqa: E402
from app.services import priorities as svc  # noqa: E402
from scripts import migrate_priorities as migration  # noqa: E402

USER = "507f1f77bcf86cd799439011"
PLAN = "507f1f77bcf86cd799439001"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _plan() -> dict:
    return {"_id": ObjectId(PLAN), "user_id": USER, "year": 2026, "deleted_at": None}


def _task(**overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "user_id": USER,
        "title": "Essay",
        "description": None,
        "is_completed": False,
        "deadline": None,
        "is_priority": True,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    doc.update(overrides)
    return doc


def _goal(milestones: list[dict], **overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "focus_area_id": "area-1",
        "title": "Finish the thesis",
        "milestones": milestones,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    doc.update(overrides)
    return doc


def _milestone(**overrides) -> dict:
    doc = {"id": str(ObjectId()), "title": "Chapter 2", "due_date": None, "completed": False, "is_key_result": False, "is_priority": False}
    doc.update(overrides)
    return doc


def _collection(docs=None, found=None) -> MagicMock:
    collection = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    collection.find_one = AsyncMock(return_value=found)
    collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1, modified_count=1))
    return collection


# --- 1. the models -------------------------------------------------------------


def test_is_priority_defaults_to_false_on_task_and_milestone():
    assert Task(title="Essay").is_priority is False
    assert Milestone(title="Chapter 2").is_priority is False
    assert Milestone(title="Chapter 2", is_priority=True).model_dump()["is_priority"] is True
    assert router.MilestonePatch().is_priority is None
    assert router.MilestoneResponse(id="m", title="t", completed=False, is_key_result=False).is_priority is False


# --- 2. flagged_items --------------------------------------------------------------


@pytest.mark.asyncio
async def test_flagged_items_shape_and_order():
    done = _task(title="Done one", is_completed=True)
    dated = _task(title="Dated one", deadline=DUE, description="soon")
    flagged = _milestone(title="Flagged step", is_priority=True)
    goal = _goal([flagged, _milestone(title="Plain step")])
    areas = _collection(docs=[{"_id": ObjectId("507f1f77bcf86cd799439031")}])
    with patch.object(svc, "tasks_collection", _collection(docs=[done, dated])), patch.object(
        svc, "focus_areas_collection", areas
    ), patch.object(svc, "goals_collection", _collection(docs=[goal])) as goals:
        items = await svc.flagged_items(USER, PLAN)

    assert [i["title"] for i in items] == ["Dated one", "Flagged step", "Done one"]
    assert [Priority(**i).linked_entity_type for i in items] == ["task", "milestone", "task"]

    task_row = items[0]
    assert task_row["_id"] == dated["_id"] and task_row["linked_entity_id"] == str(dated["_id"])
    assert task_row["annual_plan_id"] == PLAN and task_row["focus_area_id"] is None
    assert task_row["deadline"] == DUE and task_row["description"] == "soon"
    assert task_row["is_active"] is True and task_row["order"] == 0 and task_row["completed_at"] is None

    step = items[1]
    assert step["_id"] == flagged["id"] and step["linked_entity_id"] == str(goal["_id"])
    assert step["focus_area_id"] == "area-1" and step["deadline"] is None and step["description"] == ""
    assert step["created_at"] == NOW and step["is_completed"] is False

    goals.find.assert_called_once_with({"focus_area_id": {"$in": ["507f1f77bcf86cd799439031"]}, "deleted_at": None})
    goals.find.return_value.to_list.assert_awaited_once_with(length=200)
    areas.find.return_value.to_list.assert_awaited_once_with(length=10)


@pytest.mark.asyncio
async def test_flagged_items_reads_only_flagged_live_tasks_and_caps_at_fifty():
    tasks = _collection(docs=[_task(title=f"T{i}", deadline=datetime(2026, 1, 1 + i % 28, tzinfo=timezone.utc)) for i in range(60)])
    with patch.object(svc, "tasks_collection", tasks), patch.object(
        svc, "focus_areas_collection", _collection(docs=[])
    ), patch.object(svc, "goals_collection", _collection(docs=[])) as goals:
        items = await svc.flagged_items(USER, PLAN)

    assert len(items) == 50
    tasks.find.assert_called_once_with({"user_id": USER, "is_priority": True, "deleted_at": None})
    tasks.find.return_value.to_list.assert_awaited_once_with(length=200)
    goals.find.assert_not_called()


def test_milestone_due_date_and_string_deadlines_become_aware_datetimes():
    step = svc.milestone_as_priority(_milestone(due_date="2026-11-02", is_priority=True, completed=True), _goal([]), PLAN)
    assert step["deadline"] == datetime(2026, 11, 2, tzinfo=timezone.utc) and step["is_completed"] is True
    assert svc.as_datetime("2026-11-02T10:00:00Z") == datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    assert svc.as_datetime("not a date") is None and svc.as_datetime(None) is None
    # A naive stored datetime and an aware one sort together without a TypeError.
    naive = svc.task_as_priority(_task(deadline=datetime(2026, 1, 1)), PLAN)
    aware = svc.task_as_priority(_task(deadline=DUE), PLAN)
    assert [i["deadline"].year for i in svc.order_items([aware, naive])] == [2026, 2026]


# --- 3. the routes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_priorities_answers_the_flagged_list(mock_firebase_user):
    rows = [svc.task_as_priority(_task(), PLAN)]
    with patch.object(router, "annual_plans_collection", _collection(found=_plan())), patch.object(
        router, "flagged_items", AsyncMock(return_value=rows)
    ) as flagged:
        out = await router.get_priorities(annual_plan_id=PLAN, current_user=mock_firebase_user)
    assert out == rows
    flagged.assert_awaited_once_with(USER, PLAN)


@pytest.mark.asyncio
async def test_get_priorities_still_checks_plan_ownership(mock_firebase_user):
    foreign = {**_plan(), "user_id": "someone-else"}
    with patch.object(router, "annual_plans_collection", _collection(found=foreign)), patch.object(
        router, "flagged_items", AsyncMock()
    ) as flagged:
        with pytest.raises(HTTPException) as exc:
            await router.get_priorities(annual_plan_id=PLAN, current_user=mock_firebase_user)
    assert exc.value.status_code == 403
    flagged.assert_not_called()


@pytest.mark.asyncio
async def test_priority_writers_answer_410(mock_firebase_user):
    calls = [
        router.create_priority(current_user=mock_firebase_user),
        router.update_priority(id=str(ObjectId()), current_user=mock_firebase_user),
        router.delete_priority(id=str(ObjectId()), current_user=mock_firebase_user),
        router.reorder_priorities(current_user=mock_firebase_user),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 410
        assert exc.value.detail == "Priorities are a flag on tasks and milestones (ADR-046); write is_priority on the item"


def _owned_goal_patches(goal: dict):
    return (
        patch.object(router, "goals_collection", _collection(found=goal)),
        patch.object(router, "focus_areas_collection", _collection(found={"_id": "area-1", "annual_plan_id": PLAN})),
        patch.object(router, "annual_plans_collection", _collection(found=_plan())),
    )


@pytest.mark.asyncio
async def test_milestone_patch_writes_the_flag(mock_firebase_user):
    step = _milestone()
    goal = _goal([step])
    goals, areas, plans = _owned_goal_patches(goal)
    with goals as goals_mock, areas, plans:
        out = await router.update_milestone(
            goal_id=str(goal["_id"]), milestone_id=step["id"], payload=router.MilestonePatch(is_priority=True), current_user=mock_firebase_user
        )
    written = goals_mock.update_one.call_args[0][1]["$set"]["milestones.$"]
    assert written["is_priority"] is True and written["title"] == "Chapter 2"
    assert out.is_priority is True


@pytest.mark.asyncio
async def test_milestone_create_carries_the_flag(mock_firebase_user):
    goal = _goal([])
    goals, areas, plans = _owned_goal_patches(goal)
    with goals as goals_mock, areas, plans:
        out = await router.create_milestone(
            goal_id=str(goal["_id"]), payload=router.MilestoneCreate(title="Step", is_priority=True), current_user=mock_firebase_user
        )
    pushed = goals_mock.update_one.call_args[0][1]["$push"]["milestones"]
    assert pushed["is_priority"] is True and out.is_priority is True


@pytest.mark.asyncio
async def test_task_patch_refuses_a_non_boolean_flag_and_writes_a_real_one(mock_firebase_user):
    task = _task(is_priority=False)
    collection = _collection(found={**task, "is_priority": True})
    with pytest.raises(HTTPException) as exc:
        await tasks_router.update_task(id=str(task["_id"]), updates={"is_priority": "true"}, collection=collection, task=task, user=mock_firebase_user)
    assert exc.value.status_code == 400
    collection.update_one.assert_not_called()

    out = await tasks_router.update_task(id=str(task["_id"]), updates={"is_priority": True}, collection=collection, task=task, user=mock_firebase_user)
    assert collection.update_one.call_args[0][1]["$set"]["is_priority"] is True
    assert out["is_priority"] is True


# --- 4. the migration ---------------------------------------------------------------


def _priority(**overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "annual_plan_id": PLAN,
        "title": "Write chapter 2",
        "description": "",
        "linked_entity_id": None,
        "linked_entity_type": None,
        "deadline": DUE,
        "is_completed": False,
        "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc),
        "deleted_at": None,
    }
    doc.update(overrides)
    return doc


def test_builders_map_each_kind_of_link():
    task_id = ObjectId()
    goal = _goal([])
    linked_task = _priority(linked_entity_type="task", linked_entity_id=str(task_id))
    linked_goal = _priority(linked_entity_type="goal", linked_entity_id=str(goal["_id"]), is_completed=True)
    routine = _priority(linked_entity_type="routine_morning", linked_entity_id="0", description="every day")
    unlinked = _priority(deadline=None)

    flag = migration.plan_action(linked_task, True, None, USER, NOW)
    assert flag == {"kind": "flag_task", "task_id": task_id}
    assert migration.migrated_to(flag) == {"type": "task", "id": str(task_id)}

    push = migration.plan_action(linked_goal, False, goal, USER, NOW)
    assert push["kind"] == "push_milestone" and push["goal_id"] == goal["_id"]
    step = push["milestone"]
    assert step["title"] == "Write chapter 2" and step["due_date"] == "2026-10-20"
    assert step["completed"] is True and step["is_priority"] is True and step["is_key_result"] is False
    assert ObjectId.is_valid(step["id"])
    assert migration.migrated_to(push) == {"type": "milestone", "id": step["id"]}

    for priority in (routine, unlinked):
        insert = migration.plan_action(priority, False, None, USER, NOW)
        assert insert["kind"] == "insert_task"
        task = insert["task"]
        assert task["user_id"] == USER and task["is_priority"] is True and task["priority"] == "high"
        assert task["created_at"] == priority["created_at"] and task["updated_at"] == NOW
        assert task["tags"] == [] and task["category"] is None and task["deleted_at"] is None
    assert migration.plan_action(routine, False, None, USER, NOW)["task"]["description"] == "every day"
    assert migration.plan_action(unlinked, False, None, USER, NOW)["task"]["deadline"] is None
    inserted = ObjectId()
    assert migration.migrated_to({"kind": "insert_task"}, inserted) == {"type": "task", "id": str(inserted)}


def test_a_dangling_link_becomes_a_task_and_a_lost_plan_is_skipped():
    dangling = _priority(linked_entity_type="task", linked_entity_id=str(ObjectId()))
    assert migration.plan_action(dangling, False, None, USER, NOW)["kind"] == "insert_task"
    dangling_goal = _priority(linked_entity_type="goal", linked_entity_id="gone")
    assert migration.plan_action(dangling_goal, False, None, USER, NOW)["kind"] == "insert_task"
    assert migration.plan_action(dangling, False, None, None, NOW) is None


def test_needs_migration_is_false_once_stamped_or_deleted():
    assert migration.needs_migration(_priority()) is True
    assert migration.needs_migration(_priority(migrated_to={"type": "task", "id": "x"})) is False
    assert migration.needs_migration(_priority(deleted_at=NOW)) is False
    assert migration.due_key("2026-03-04") == "2026-03-04" and migration.due_key(None) is None


@pytest.mark.asyncio
async def test_runner_flags_the_task_stamps_the_priority_and_skips_a_migrated_one():
    task = _task(is_priority=False)
    linked = _priority(linked_entity_type="task", linked_entity_id=str(task["_id"]))
    stamped = _priority(migrated_to={"type": "task", "id": "x"})
    priorities = _collection(docs=[linked, stamped])
    with patch.object(migration, "priorities_collection", priorities), patch.object(
        migration, "tasks_collection", _collection(found=task)
    ) as tasks, patch.object(migration, "goals_collection", _collection()) as goals, patch.object(
        migration, "annual_plans_collection", _collection(found=_plan())
    ):
        counts = await migration.migrate(now=NOW)

    assert counts == {"tasks_flagged": 1, "milestones_added": 0, "tasks_created": 0, "orphaned": 0, "skipped": 1}
    priorities.find.assert_called_once_with({"deleted_at": None, "migrated_to": {"$exists": False}})
    tasks.update_one.assert_awaited_once_with({"_id": task["_id"]}, {"$set": {"is_priority": True, "updated_at": NOW}})
    priorities.update_one.assert_awaited_once_with(
        {"_id": linked["_id"]}, {"$set": {"migrated_to": {"type": "task", "id": str(task["_id"])}, "migrated_at": NOW}}
    )
    goals.update_one.assert_not_called()
    tasks.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_runner_pushes_a_milestone_creates_a_task_and_a_dry_run_writes_nothing(capsys):
    goal = _goal([])
    by_goal = _priority(linked_entity_type="goal", linked_entity_id=str(goal["_id"]))
    by_routine = _priority(linked_entity_type="routine_evening", linked_entity_id="2")
    priorities = _collection(docs=[by_goal, by_routine])
    patches = lambda: (  # noqa: E731
        patch.object(migration, "priorities_collection", priorities),
        patch.object(migration, "tasks_collection", _collection()),
        patch.object(migration, "goals_collection", _collection(found=goal)),
        patch.object(migration, "annual_plans_collection", _collection(found=_plan())),
    )
    p, t, g, a = patches()
    with p, t as tasks, g as goals, a:
        counts = await migration.migrate(now=NOW)
    assert counts["milestones_added"] == 1 and counts["tasks_created"] == 1
    assert goals.update_one.call_args[0][1]["$push"]["milestones"]["is_priority"] is True
    assert tasks.insert_one.call_args[0][0]["user_id"] == USER
    assert priorities.update_one.await_count == 2

    priorities = _collection(docs=[by_goal, by_routine])
    p, t, g, a = patches()
    with p, t as tasks, g as goals, a:
        counts = await migration.migrate(dry_run=True, now=NOW)
    assert counts["milestones_added"] == 1 and counts["tasks_created"] == 1
    tasks.insert_one.assert_not_called()
    goals.update_one.assert_not_called()
    priorities.update_one.assert_not_called()
    assert "milestone on goal" in capsys.readouterr().out
