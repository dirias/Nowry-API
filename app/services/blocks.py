"""Blocks: occurrence expansion and the lazy migration (PLAN-001, ADR-045).

Everything that can be pure is pure and sits above the line; the migration,
which reads the legacy collections, sits below it and is the only thing here
that touches the database.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.config.database import (
    activities_collection,
    annual_plans_collection,
    block_log_collection,
    blocks_collection,
    daily_routines_collection,
    focus_areas_collection,
    goals_collection,
)

#: The three periods survive as default starts, not as objects (PRD D1).
PERIOD_STARTS = {"morning": "07:00", "afternoon": "13:00", "evening": "20:00", "anytime": "13:00"}
PERIOD_ORDER = ("morning", "afternoon", "evening")
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]
MIGRATED_ROUTINE_LENGTH = 10
MAX_RANGE_DAYS = 62
#: Bounded reads (CLAUDE.md): a learner has tens of blocks, not thousands.
MAX_BLOCKS = 500
MAX_LOGS = 5000


# --- pure -------------------------------------------------------------------


def parse_range(from_: str, to: str) -> tuple[date, date]:
    """Two local date strings, in order, at most MAX_RANGE_DAYS apart."""
    try:
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="from and to must be YYYY-MM-DD") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="to must not be before from")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"a range covers at most {MAX_RANGE_DAYS} days")
    return start, end


def add_minutes(start: str, minutes: int) -> str:
    """'07:00' + 25 → '07:25'; wraps at midnight, which a migration never reaches."""
    hours, mins = (int(part) for part in start.split(":"))
    total = (hours * 60 + mins + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def round_length(minutes: Optional[int]) -> int:
    """To the 5-minute grid, never under 5."""
    value = int(minutes or 0)
    return max(5, int(round(value / 5.0)) * 5)


def occurs_on(block: dict, day: date) -> bool:
    if block.get("deleted_at"):
        return False
    if block.get("date"):
        return block["date"] == day.isoformat()
    return day.weekday() in (block.get("days_of_week") or [])


def expand_occurrences(blocks: Iterable[dict], start: date, end: date) -> list[dict]:
    """Every (day, block) in the range, ordered by day, then start, then title."""
    rows: list[dict] = []
    day = start
    while day <= end:
        for block in blocks:
            if occurs_on(block, day):
                rows.append(
                    {
                        "date": day.isoformat(),
                        "block_id": str(block["_id"]),
                        "start": block["start"],
                        "length_min": block["length_min"],
                        "title": block.get("title", ""),
                    }
                )
        day += timedelta(days=1)
    rows.sort(key=lambda row: (row["date"], row["start"], row["title"]))
    return rows


def _item_title(item: dict) -> str:
    return str(item.get("title") or item.get("text") or item.get("activity") or "").strip()


def routine_blocks(routine: dict, now: datetime) -> tuple[list[dict], dict[str, int]]:
    """A routine document → daily blocks, one per item, in sequence per period.

    Returns the documents to insert and a map from the item's legacy id to its
    index in that list, which the completions migration needs.
    """
    docs: list[dict] = []
    index_by_item: dict[str, int] = {}
    for period in PERIOD_ORDER:
        for position, item in enumerate(routine.get(f"{period}_routine") or []):
            title = _item_title(item)
            item_id = item.get("id")
            if not title or not item_id:
                continue
            index_by_item[str(item_id)] = len(docs)
            docs.append(
                _new_block_doc(
                    routine["user_id"],
                    title,
                    {"type": "routine", "id": None},
                    add_minutes(PERIOD_STARTS[period], position * MIGRATED_ROUTINE_LENGTH),
                    MIGRATED_ROUTINE_LENGTH,
                    EVERY_DAY,
                    {"collection": "daily_routines", "id": str(item_id)},
                    now,
                )
            )
    return docs, index_by_item


def activity_block(activity: dict, goal: dict, now: datetime) -> Optional[dict]:
    """A goal activity → one block whose source is its goal."""
    title = str(activity.get("title") or "").strip()
    if not title or activity.get("deleted_at") or activity.get("is_active") is False:
        return None
    frequency = activity.get("frequency") or "daily"
    days = EVERY_DAY if frequency == "daily" else (activity.get("days_of_week") or EVERY_DAY)
    period = activity.get("time_of_day") or "anytime"
    return _new_block_doc(
        goal["user_id"],
        title,
        {"type": "goal", "id": str(goal["_id"])},
        PERIOD_STARTS.get(period, PERIOD_STARTS["anytime"]),
        round_length(activity.get("duration_minutes")),
        sorted(set(int(d) for d in days)),
        {"collection": "activities", "id": str(activity["_id"])},
        now,
        area_id=str(goal["focus_area_id"]) if goal.get("focus_area_id") else None,
    )


def _new_block_doc(
    user_id: str,
    title: str,
    source: dict,
    start: str,
    length_min: int,
    days: list[int],
    migrated_from: dict,
    now: datetime,
    area_id: Optional[str] = None,
) -> dict:
    return {
        "_id": ObjectId(),
        "user_id": user_id,
        "title": title[:120],
        "source": source,
        "start": start,
        "length_min": length_min,
        "days_of_week": days,
        "date": None,
        "reminder_min": None,
        "after_block_id": None,
        "area_id": area_id,
        "migrated_from": migrated_from,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "deleted_by": None,
    }


def completion_logs(routine: dict, block_docs: list[dict], index_by_item: dict[str, int], now: datetime) -> list[dict]:
    """`daily_completions[date] = [item ids]` → kept rows for the migrated blocks."""
    rows: list[dict] = []
    for date_key, item_ids in (routine.get("daily_completions") or {}).items():
        if not isinstance(item_ids, list):
            continue
        for item_id in item_ids:
            index = index_by_item.get(str(item_id))
            if index is None:
                continue
            block = block_docs[index]
            rows.append(
                {
                    "_id": ObjectId(),
                    "user_id": routine["user_id"],
                    "block_id": str(block["_id"]),
                    "date": date_key,
                    "status": "kept",
                    "started_at": None,
                    "ended_at": None,
                    "minutes": block["length_min"],
                    "record": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    return rows


# --- the migration (reads the legacy collections) ---------------------------


async def _user_goals(user_id: str) -> list[dict]:
    """Goals belong to a user through focus area → annual plan (no user_id on them)."""
    plans = await annual_plans_collection.find({"user_id": user_id}, {"_id": 1}).to_list(length=50)
    if not plans:
        return []
    plan_ids = [str(p["_id"]) for p in plans]
    areas = await focus_areas_collection.find({"annual_plan_id": {"$in": plan_ids}}, {"_id": 1}).to_list(length=100)
    if not areas:
        return []
    area_ids = [str(a["_id"]) for a in areas]
    goals = await goals_collection.find({"focus_area_id": {"$in": area_ids}, "deleted_at": None}).to_list(length=500)
    for goal in goals:
        goal["user_id"] = user_id
    return goals


async def migrate_legacy(user_id: str, now: Optional[datetime] = None) -> dict[str, int]:
    """PRD D10: a user's routine items, their completions and their goal
    activities become blocks and log rows. Called once, when the user has no
    blocks at all; `migrated_from` keeps a second run from doubling anything.
    """
    now = now or datetime.now(timezone.utc)
    block_docs: list[dict] = []
    log_rows: list[dict] = []

    routine = await daily_routines_collection.find_one({"user_id": user_id})
    if routine:
        routine["user_id"] = user_id
        docs, index_by_item = routine_blocks(routine, now)
        block_docs.extend(docs)
        log_rows.extend(completion_logs(routine, docs, index_by_item, now))

    goals = await _user_goals(user_id)
    if goals:
        by_id = {str(g["_id"]): g for g in goals}
        activities = await activities_collection.find({"goal_id": {"$in": list(by_id)}}).to_list(length=500)
        for activity in activities:
            doc = activity_block(activity, by_id[str(activity["goal_id"])], now)
            if doc:
                block_docs.append(doc)

    if block_docs:
        already = await blocks_collection.find(
            {"user_id": user_id, "migrated_from.id": {"$in": [d["migrated_from"]["id"] for d in block_docs]}},
            {"migrated_from.id": 1},
        ).to_list(length=MAX_BLOCKS)
        done = {a["migrated_from"]["id"] for a in already}
        block_docs = [d for d in block_docs if d["migrated_from"]["id"] not in done]
        kept_ids = {str(d["_id"]) for d in block_docs}
        log_rows = [r for r in log_rows if r["block_id"] in kept_ids]
    if block_docs:
        await blocks_collection.insert_many(block_docs)
    if log_rows:
        await block_log_collection.insert_many(log_rows)
    return {"blocks": len(block_docs), "logs": len(log_rows)}
