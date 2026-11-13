"""
What deleting an account removes, and when (ADR-081, docs/prd-road-to-market.md FR-029).

One list names every collection an account owns and the field that says so.
Deletion marks every live row of each with `deleted_at`; the database erases a
marked row 30 days later (the `soft_delete_ttl` index in `config/database.py`
is created from the same list), the account record included. Rows that hang
off a plan are found through the plan.

The account record also gives up its identity at once: its email, username
and sign-in id become placeholders that cannot collide, and the real ones wait
under `deleted_identity` for the 30 days, for support's eyes only. So the same
address can make a new account the same day, and nothing old ever attaches to
it: every old row carries the old account's id, which no one can sign in as.

    mark_everything(user_id, now)     every owned row, one call per collection
    release_identity(user, now)       the account record, last
    delete_stripe_customer(id)        never raises
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId

from app.config import database
from app.utils.logger import get_logger

logger = get_logger(__name__)

RETENTION_DAYS = 30

#: (collection, owner field). Every collection a row of which belongs to one account.
OWNED: Tuple[Tuple[str, str], ...] = (
    ("books", "user_id"),
    ("decks", "user_id"),
    ("cards", "user_id"),
    ("card_attempts", "user_id"),
    ("study_sessions", "user_id"),
    ("tag_settings", "user_id"),
    ("tasks", "user_id"),
    ("task_lists", "user_id"),
    ("daily_routines", "user_id"),
    ("blocks", "user_id"),
    ("block_log", "user_id"),
    ("annual_plans", "user_id"),
    ("quarter_reports", "user_id"),
    ("book_chunks", "user_id"),
    ("sheets", "user_id"),
    ("comments", "user_id"),
    ("blackboards", "owner_user_id"),
)

#: Collections whose rows hang off a plan, a focus area or a goal rather than an account.
PLAN_CHILDREN: Tuple[str, ...] = ("focus_areas", "goals", "priorities", "activities")

#: Every collection that carries the 30-day expiry: the owned ones, the plan's children, the account record.
EXPIRING: Tuple[str, ...] = tuple(name for name, _ in OWNED) + PLAN_CHILDREN + ("users",)

#: Collections that hold an account's id on purpose and are handled otherwise.
#: Read by a test, so a new collection must be placed in one list or the other.
HANDLED_OTHERWISE: Dict[str, str] = {
    "device_tokens": "removed at once: an address, not content",
    "media": "the media ledger condemns the account's images and sweeps them",
    "quiz_sessions": "expires one hour after it is written",
    "ai_quiz_sessions": "expires one day after it is written",
    "invites": "the operator's record of who was invited; the account id it names is gone with the account",
    "bugs": "a report the person chose to send to the operator",
    "content_forks": "legacy, read by one migration only",
    "ai_traces": "two ids per Langfuse trace; the account's rows go when its traces are deleted at Langfuse, at once (ai_traces.py)",
    "ai_trace_deletions": "a deletion Langfuse refused, retried until it succeeds; holds only the account id",
}

#: Collections that may hold `is_public`; deletion unpublishes as it marks.
PUBLISHABLE = frozenset({"books", "decks", "cards"})


def _mark(name: str, now: datetime, user_id: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"deleted_at": now, "deleted_by": user_id, "updated_at": now}
    if name in PUBLISHABLE:
        fields["is_public"] = False
    return {"$set": fields}


def _ids(rows: List[dict]) -> List[str]:
    return [str(row["_id"]) for row in rows]


async def _plan_children(user_id: str, now: datetime) -> Dict[str, int]:
    """Focus areas, goals, priorities and activities, found through the account's plans."""
    db = database.db
    marked: Dict[str, int] = {}
    plans = _ids(await db["annual_plans"].find({"user_id": user_id}, {"_id": 1}).to_list(length=100))
    if not plans:
        return marked
    areas = _ids(await db["focus_areas"].find({"annual_plan_id": {"$in": plans}}, {"_id": 1}).to_list(length=1000))
    goals = _ids(await db["goals"].find({"focus_area_id": {"$in": areas}}, {"_id": 1}).to_list(length=5000)) if areas else []
    targets = [
        ("focus_areas", {"annual_plan_id": {"$in": plans}}),
        ("goals", {"focus_area_id": {"$in": areas}}),
        ("priorities", {"$or": [{"annual_plan_id": {"$in": plans}}, {"focus_area_id": {"$in": areas}}]}),
        ("activities", {"goal_id": {"$in": goals}}),
    ]
    for name, query in targets:
        result = await db[name].update_many({**query, "deleted_at": None}, _mark(name, now, user_id))
        marked[name] = result.modified_count
    return marked


async def mark_everything(user_id: str, now: datetime) -> Dict[str, int]:
    """Mark every live row the account owns. Returns how many rows each collection marked."""
    db = database.db
    marked: Dict[str, int] = {}
    # The plan's children first: they are found through plans, which are marked below.
    marked.update(await _plan_children(user_id, now))
    for name, owner in OWNED:
        result = await db[name].update_many({owner: user_id, "deleted_at": None}, _mark(name, now, user_id))
        marked[name] = result.modified_count
    return marked


def placeholder_identity(user_id: str) -> Dict[str, str]:
    """Values that satisfy the unique indexes and can never be anyone's."""
    return {
        "email": f"deleted-{user_id}@deleted.nowry.invalid",
        "username": f"deleted-{user_id}",
        "firebase_uid": f"deleted-{user_id}",
    }


async def release_identity(user: dict, now: datetime, keep_existing: bool = False) -> None:
    """Mark the account record and give up its email, username and sign-in id at once.

    `keep_existing` is for a record deleted before ADR-081, released later:
    its deletion time and anything already kept aside stay as they are.
    """
    user_id = str(user["_id"])
    kept = user.get("deleted_identity") if keep_existing and user.get("deleted_identity") else {
        key: user.get(key) for key in ("email", "username", "firebase_uid", "stripe_customer_id")
    }
    await database.db["users"].update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                **placeholder_identity(user_id),
                "deleted_identity": kept,
                "stripe_customer_id": None,
                "deleted_at": now,
                "deleted_by": user_id,
                "updated_at": now,
            }
        },
    )


async def delete_stripe_customer(customer_id: Optional[str]) -> bool:
    """Delete the account's Stripe customer. False when there is none or Stripe refused; never raises."""
    if not customer_id or not os.getenv("STRIPE_SECRET_KEY"):
        return False
    try:
        import stripe

        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
        await asyncio.to_thread(stripe.Customer.delete, customer_id)
        return True
    except Exception as exc:  # noqa: BLE001 - a Stripe failure must not undo a deletion
        logger.error("account deletion: the Stripe customer could not be deleted: %s", type(exc).__name__)
        return False
