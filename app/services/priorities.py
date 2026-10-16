"""Priorities as a flag (PLAN-013, ADR-046 §5).

A priority is no longer a document the learner creates: it is `is_priority`
on a task or on a milestone. The lists that used to read the `priorities`
collection — Overview's Priorities, Home's band, the calendar's filter — keep
reading the old Priority shape, so this module projects every flagged item
into it. The builders above the line are pure; `flagged_items` reads Mongo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.config.database import focus_areas_collection, goals_collection, tasks_collection

MAX_ITEMS = 50
MAX_TASKS = 200
MAX_GOALS = 200
MAX_AREAS = 10


# --- pure builders ------------------------------------------------------------


def as_datetime(value: Any) -> Optional[datetime]:
    """A stored deadline — datetime, 'YYYY-MM-DD' or ISO string — as an aware datetime, else None."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamps(doc: dict) -> tuple[datetime, datetime]:
    """(created_at, updated_at) with each falling back to the other, then to now."""
    created = doc.get("created_at") or doc.get("updated_at") or datetime.now(timezone.utc)
    updated = doc.get("updated_at") or created
    return created, updated


def task_as_priority(task: dict, plan_id: str) -> dict:
    """A flagged task in the Priority document shape."""
    created, updated = _stamps(task)
    return {
        "_id": task["_id"],
        "annual_plan_id": plan_id,
        "focus_area_id": None,
        "title": task.get("title", ""),
        "description": task.get("description") or "",
        "linked_entity_id": str(task["_id"]),
        "linked_entity_type": "task",
        "deadline": as_datetime(task.get("deadline")),
        "order": 0,
        "is_active": True,
        "is_completed": bool(task.get("is_completed", False)),
        "created_at": created,
        "updated_at": updated,
        "completed_at": None,
    }


def milestone_as_priority(milestone: dict, goal: dict, plan_id: str) -> dict:
    """A flagged milestone of a goal in the Priority document shape."""
    created, updated = _stamps(goal)
    return {
        "_id": milestone["id"],
        "annual_plan_id": plan_id,
        "focus_area_id": goal.get("focus_area_id"),
        "title": milestone.get("title", ""),
        "description": "",
        "linked_entity_id": str(goal["_id"]),
        "linked_entity_type": "milestone",
        "deadline": as_datetime(milestone.get("due_date")),
        "order": 0,
        "is_active": True,
        "is_completed": bool(milestone.get("completed", False)),
        "created_at": created,
        "updated_at": updated,
        "completed_at": None,
    }


def flagged_milestones(goals: list[dict], plan_id: str) -> list[dict]:
    """Every flagged milestone of the given goals, in the Priority shape."""
    return [
        milestone_as_priority(milestone, goal, plan_id)
        for goal in goals
        for milestone in goal.get("milestones") or []
        if milestone.get("is_priority") and milestone.get("id")
    ]


def _sort_key(item: dict) -> tuple[bool, bool, float]:
    deadline = item.get("deadline")
    return (item["is_completed"], deadline is None, deadline.timestamp() if deadline else 0.0)


def order_items(items: list[dict], cap: int = MAX_ITEMS) -> list[dict]:
    """Open before completed; within each, dated by deadline ascending, then undated; capped."""
    return sorted(items, key=_sort_key)[:cap]


# --- the reader ---------------------------------------------------------------


async def _plan_goals(plan_id: str) -> list[dict]:
    """The plan's live goals, through its focus areas (mirrors /annual-plan/full)."""
    areas = await focus_areas_collection.find(
        {"annual_plan_id": plan_id, "deleted_at": None}, {"_id": 1}
    ).to_list(length=MAX_AREAS)
    if not areas:
        return []
    area_ids = [str(a["_id"]) for a in areas]
    return await goals_collection.find(
        {"focus_area_id": {"$in": area_ids}, "deleted_at": None}
    ).to_list(length=MAX_GOALS)


async def flagged_items(user_id: str, plan_id: str) -> list[dict]:
    """Every flagged task of the user and flagged milestone of the plan, as priorities."""
    tasks = await tasks_collection.find(
        {"user_id": user_id, "is_priority": True, "deleted_at": None}
    ).to_list(length=MAX_TASKS)
    goals = await _plan_goals(plan_id)
    items = [task_as_priority(t, plan_id) for t in tasks] + flagged_milestones(goals, plan_id)
    return order_items(items)
