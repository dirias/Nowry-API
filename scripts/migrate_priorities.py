"""Migrate the yearly `priorities` collection to the priority flag (PLAN-013, ADR-046 §5).

A priority used to be a document a learner created and linked to a goal, a
task or a routine slot. It is now `is_priority` on a task or a milestone. For
every live priority that has not been migrated yet:

  - linked to a task that exists   → the task is flagged;
  - linked to a goal that exists   → a flagged milestone is pushed onto the goal
                                     (a priority tied to a goal is a step of it
                                     the learner cared about);
  - anything else (routine_*, unlinked, or a dangling link)
                                   → a flagged task is created for the plan's owner.

The priority is then stamped `migrated_to` / `migrated_at`, which makes a
second run a no-op. The builders above the line are pure; `migrate` reads and
writes Mongo.

Usage (run from Nowry-API/):
    .venv/bin/python -m scripts.migrate_priorities              # migrate
    .venv/bin/python -m scripts.migrate_priorities --dry-run    # print the plan, write nothing
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bson import ObjectId

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.config.database import (  # noqa: E402
    annual_plans_collection,
    goals_collection,
    priorities_collection,
    tasks_collection,
)
from app.services.priorities import as_datetime  # noqa: E402

MAX_PRIORITIES = 5000

FLAG_TASK = "flag_task"
PUSH_MILESTONE = "push_milestone"
INSERT_TASK = "insert_task"


# --- pure builders ------------------------------------------------------------


def needs_migration(priority: dict) -> bool:
    """Live and never migrated: `deleted_at` None and no `migrated_to`."""
    return priority.get("deleted_at") is None and "migrated_to" not in priority


def object_id(value: Any) -> Optional[ObjectId]:
    """The value as an ObjectId when it is one, else None."""
    if isinstance(value, ObjectId):
        return value
    return ObjectId(value) if isinstance(value, str) and ObjectId.is_valid(value) else None


def due_key(deadline: Any) -> Optional[str]:
    """A priority's deadline as a milestone's 'YYYY-MM-DD', or None."""
    parsed = as_datetime(deadline)
    return parsed.date().isoformat() if parsed else None


def milestone_from_priority(priority: dict) -> dict:
    """The flagged milestone a goal-linked priority becomes."""
    return {
        "id": str(ObjectId()),
        "title": priority.get("title", ""),
        "due_date": due_key(priority.get("deadline")),
        "completed": bool(priority.get("is_completed", False)),
        "is_key_result": False,
        "is_priority": True,
    }


def task_from_priority(priority: dict, user_id: str, now: datetime) -> dict:
    """The flagged task an unlinked, routine-linked or dangling priority becomes."""
    return {
        "user_id": user_id,
        "title": priority.get("title", ""),
        "description": priority.get("description") or "",
        "is_completed": bool(priority.get("is_completed", False)),
        "priority": "high",
        "deadline": as_datetime(priority.get("deadline")),
        "tags": [],
        "category": None,
        "is_priority": True,
        "created_at": priority.get("created_at") or now,
        "updated_at": now,
        "deleted_at": None,
        "deleted_by": None,
    }


def plan_action(
    priority: dict,
    task_found: bool,
    goal: Optional[dict],
    owner_id: Optional[str],
    now: datetime,
) -> Optional[dict]:
    """What one priority becomes, given what its link resolved to.

    `task_found` and `goal` are the resolved link (a dangling link resolves to
    nothing and falls through); `owner_id` is the plan's user. None when the
    plan itself is gone, since a task needs an owner.
    """
    link_type = priority.get("linked_entity_type")
    if link_type == "task" and task_found:
        return {"kind": FLAG_TASK, "task_id": object_id(priority.get("linked_entity_id"))}
    if link_type == "goal" and goal is not None:
        return {"kind": PUSH_MILESTONE, "goal_id": goal["_id"], "milestone": milestone_from_priority(priority)}
    if owner_id is None:
        return None
    return {"kind": INSERT_TASK, "task": task_from_priority(priority, owner_id, now)}


def migrated_to(action: dict, inserted_id: Any = None) -> dict:
    """The `migrated_to` stamp for an applied action."""
    if action["kind"] == FLAG_TASK:
        return {"type": "task", "id": str(action["task_id"])}
    if action["kind"] == PUSH_MILESTONE:
        return {"type": "milestone", "id": action["milestone"]["id"]}
    return {"type": "task", "id": str(inserted_id)}


def describe(priority: dict, action: Optional[dict]) -> str:
    """One line of the dry-run plan."""
    title = priority.get("title", "")
    if action is None:
        return f"{priority.get('_id')} '{title}' -> SKIP: plan {priority.get('annual_plan_id')} not found"
    if action["kind"] == FLAG_TASK:
        return f"{priority.get('_id')} '{title}' -> flag task {action['task_id']}"
    if action["kind"] == PUSH_MILESTONE:
        return f"{priority.get('_id')} '{title}' -> milestone on goal {action['goal_id']}"
    return f"{priority.get('_id')} '{title}' -> new task for user {action['task']['user_id']}"


# --- the runner (reads and writes Mongo) --------------------------------------


async def _find_by_id(collection: Any, raw_id: Any, live_only: bool = True) -> Optional[dict]:
    """A document by ObjectId, falling back to a string `_id` as the routers do."""
    if not raw_id:
        return None
    base = {"deleted_at": None} if live_only else {}
    oid = object_id(raw_id)
    doc = await collection.find_one({"_id": oid, **base}) if oid else None
    if doc is None:
        doc = await collection.find_one({"_id": raw_id, **base})
    return doc


async def _resolve(priority: dict) -> tuple[bool, Optional[dict], Optional[str]]:
    """(task exists, the goal, the plan owner's user_id) for one priority's link."""
    link_type = priority.get("linked_entity_type")
    link_id = priority.get("linked_entity_id")
    task_found = False
    goal = None
    if link_type == "task":
        task_found = await _find_by_id(tasks_collection, link_id) is not None
    elif link_type == "goal":
        goal = await _find_by_id(goals_collection, link_id)
    plan = await _find_by_id(annual_plans_collection, priority.get("annual_plan_id"), live_only=False)
    owner_id = str(plan["user_id"]) if plan and plan.get("user_id") else None
    return task_found, goal, owner_id


async def _apply(action: dict, now: datetime) -> dict:
    """Write one action and return its `migrated_to` stamp."""
    if action["kind"] == FLAG_TASK:
        await tasks_collection.update_one(
            {"_id": action["task_id"]}, {"$set": {"is_priority": True, "updated_at": now}}
        )
        return migrated_to(action)
    if action["kind"] == PUSH_MILESTONE:
        await goals_collection.update_one(
            {"_id": action["goal_id"]},
            {"$push": {"milestones": action["milestone"]}, "$set": {"updated_at": now}},
        )
        return migrated_to(action)
    result = await tasks_collection.insert_one(action["task"])
    return migrated_to(action, result.inserted_id)


def _count_key(action: Optional[dict]) -> str:
    if action is None:
        return "orphaned"
    return {FLAG_TASK: "tasks_flagged", PUSH_MILESTONE: "milestones_added", INSERT_TASK: "tasks_created"}[action["kind"]]


async def migrate(dry_run: bool = False, now: Optional[datetime] = None) -> dict[str, int]:
    """Migrate every live, unmigrated priority; print the plan instead when `dry_run`."""
    now = now or datetime.now(timezone.utc)
    counts = {"tasks_flagged": 0, "milestones_added": 0, "tasks_created": 0, "orphaned": 0, "skipped": 0}
    priorities = await priorities_collection.find(
        {"deleted_at": None, "migrated_to": {"$exists": False}}
    ).to_list(length=MAX_PRIORITIES)
    for priority in priorities:
        if not needs_migration(priority):
            counts["skipped"] += 1
            continue
        task_found, goal, owner_id = await _resolve(priority)
        action = plan_action(priority, task_found, goal, owner_id, now)
        counts[_count_key(action)] += 1
        if dry_run or action is None:
            print(describe(priority, action))
            continue
        stamp = await _apply(action, now)
        await priorities_collection.update_one(
            {"_id": priority["_id"]}, {"$set": {"migrated_to": stamp, "migrated_at": now}}
        )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate yearly priorities to the is_priority flag (ADR-046).")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    args = parser.parse_args()
    counts = asyncio.run(migrate(dry_run=args.dry_run))
    label = "would be" if args.dry_run else "were"
    print(f"Priorities {label} migrated:")
    for key, value in counts.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
