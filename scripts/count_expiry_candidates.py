"""
Read-only: what a 30-day expiry on `deleted_at` would erase today (ACCOUNT-001, release 2).

For each collection the account-deletion fix would put under the expiry, this
counts the rows whose `deleted_at` is more than 30 days old: the rows the
database would remove within a minute of the index being created. Each count is
split into rows of accounts that were deleted and rows someone deleted one at a
time from an account that still exists.

The collections already under the expiry (books, decks, cards, tasks, annual
plans, goals, sheets, boards, comments) are listed too, for completeness: they
are expiring today and the fix changes nothing for them.

Nothing is written. No address, name or content is printed: counts only.

The split by owner reads `user_id`. A collection whose rows hang off a parent
instead (focus areas under a plan, activities under a goal) counts every row
as "deleted one at a time"; the total is right either way.

    railway run --environment development --service Nowry -- .venv/bin/python -m scripts.count_expiry_candidates
    railway run --environment Production  --service Nowry -- .venv/bin/python -m scripts.count_expiry_candidates
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set

from bson import ObjectId

from app.config.database import db

RETENTION = timedelta(days=30)

#: Would come under the expiry with the fix. Owner field per collection.
NEW: Dict[str, str] = {
    "users": "_id",
    "study_sessions": "user_id",
    "focus_areas": "user_id",
    "priorities": "user_id",
    "activities": "user_id",
    "daily_routines": "user_id",
    "card_attempts": "user_id",
    "tag_settings": "user_id",
    "task_lists": "user_id",
    "blocks": "user_id",
    "block_log": "user_id",
    "quarter_reports": "user_id",
    "book_chunks": "user_id",
    "content_forks": "user_id",
}

#: Already under the expiry today (database.py `_collection_ttl_map`).
EXISTING: List[str] = ["books", "decks", "cards", "tasks", "annual_plans", "goals", "sheets", "blackboards", "comments"]


async def deleted_account_ids() -> Set[str]:
    rows = await db["users"].find({"deleted_at": {"$ne": None}}, {"_id": 1}).to_list(length=100_000)
    return {str(row["_id"]) for row in rows}


async def count(collection: str, owner_field: str, cutoff: datetime, gone: Set[str]) -> Dict[str, int]:
    query = {"deleted_at": {"$ne": None, "$lte": cutoff}}
    total = await db[collection].count_documents(query)
    if owner_field == "_id":
        return {"total": total, "of_deleted_accounts": total, "deleted_one_at_a_time": 0}
    owners = await db[collection].distinct(owner_field, query)
    of_gone = 0
    for owner in owners:
        key = str(owner) if isinstance(owner, ObjectId) else owner
        if key in gone:
            of_gone += await db[collection].count_documents({**query, owner_field: owner})
    return {"total": total, "of_deleted_accounts": of_gone, "deleted_one_at_a_time": total - of_gone}


async def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - RETENTION
    gone = await deleted_account_ids()
    print(f"as of {now:%Y-%m-%d %H:%M} UTC; erased = deleted_at on or before {cutoff:%Y-%m-%d}")
    print(f"deleted accounts on record (any age): {len(gone)}")
    print("\nWOULD BE ERASED when the fix creates the expiry:")
    grand = 0
    for collection, owner in NEW.items():
        c = await count(collection, owner, cutoff, gone)
        grand += c["total"]
        print(f"  {collection:<16} {c['total']:>6}   of deleted accounts {c['of_deleted_accounts']:>6}   deleted one at a time {c['deleted_one_at_a_time']:>6}")
    print(f"  {'TOTAL':<16} {grand:>6}")
    print("\nALREADY EXPIRING today (unchanged by the fix):")
    for collection in EXISTING:
        pending = await db[collection].count_documents({"deleted_at": {"$ne": None}})
        print(f"  {collection:<16} {pending:>6} soft-deleted rows waiting for their 30 days")
    print("\nRows of deleted accounts the fix must FIRST mark (never marked by today's deletion):")
    for collection, owner in NEW.items():
        if owner == "_id" or not gone:
            continue
        unmarked = await db[collection].count_documents({owner: {"$in": list(gone)}, "deleted_at": None})
        if unmarked:
            print(f"  {collection:<16} {unmarked:>6}")


if __name__ == "__main__":
    asyncio.run(main())
