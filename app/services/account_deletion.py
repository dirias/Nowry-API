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

ADR-089 adds three things around it: a deletion needs a sign-in less than five
minutes old (`signed_in_recently`), the address is told once afterwards
(`send_deletion_notice`), and a new account with the same address learns of
the old one within the 30 days (`previous_account`).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId

from app.config import database
from app.services.email import send_email
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


# ── ADR-089: a fresh sign-in, the notice, the re-registration notice ────────

#: How old a sign-in may be when the account is deleted (FR-038).
RECENT_SIGN_IN_SECONDS = 300

#: The refusal the clients answer by asking for the sign-in again.
REAUTH_REQUIRED_CODE = "reauth_required"


def signed_in_recently(auth_time: Any, now_epoch: Optional[float] = None) -> bool:
    """Whether the token's `auth_time` (seconds since the epoch) is less than five minutes old.

    A token without one is refused: every Firebase ID token carries it.
    """
    if not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool):
        return False
    now_epoch = time.time() if now_epoch is None else now_epoch
    return now_epoch - auth_time <= RECENT_SIGN_IN_SECONDS


def notice_locale(requested: Optional[str], user: dict) -> Optional[str]:
    """The language of the notice: the client's, else the stored one; the renderer falls back to English."""
    stored = ((user.get("preferences") or {}).get("general") or {}).get("language")
    return (requested or "").strip() or stored or None


async def send_deletion_notice(user: dict, locale: Optional[str], now: datetime) -> str:
    """Tell the account's address that it was deleted (FR-039), once, and store the outcome.

    `user` is the record as it was before its identity was released. Only a
    verified address is written to (ADR-076). Never raises.
    """
    address = (user.get("email") or "").strip()
    if user.get("email_verified") is not True or not address:
        outcome = "unverified"
    else:
        try:
            outcome = await send_email("account.deleted", address, locale, {})
        except Exception as exc:  # noqa: BLE001 - the deletion is done; a notice failure must not surface
            logger.error("account deletion: the notice could not be sent: %s", type(exc).__name__)
            outcome = "failed"
    await database.db["users"].update_one(
        {"_id": user["_id"]}, {"$set": {"deletion_notice": {"outcome": outcome, "at": now}}}
    )
    return outcome


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def previous_account(user: dict, now: Optional[datetime] = None) -> Optional[Dict[str, str]]:
    """The deletion this new account should hear about (FR-041), or None.

    Only for a verified address, an account created after that deletion, a
    deletion less than 30 days old, and while the notice is not dismissed.
    """
    if user.get("email_verified") is not True or not user.get("email"):
        return None
    if (user.get("notices") or {}).get("previous_account_dismissed_at"):
        return None
    now = now or datetime.now(timezone.utc)
    window: Dict[str, Any] = {"$gte": now - timedelta(days=RETENTION_DAYS)}
    created = user.get("created_at")
    if isinstance(created, datetime):
        window["$lte"] = _aware(created)
    query = {
        "deleted_identity.email": {"$regex": f"^{re.escape(user['email'].strip())}$", "$options": "i"},
        "deleted_at": window,
        "_id": {"$ne": user["_id"]},
    }
    rows = await database.db["users"].find(query, {"deleted_at": 1}).sort("deleted_at", -1).to_list(length=1)
    if not rows:
        return None
    deleted_at = _aware(rows[0]["deleted_at"])
    if deleted_at < now - timedelta(days=RETENTION_DAYS):
        return None
    return {
        "deleted_at": deleted_at.isoformat(),
        "erased_at": (deleted_at + timedelta(days=RETENTION_DAYS)).isoformat(),
    }


async def dismiss_previous_account(user_id: str, now: datetime) -> None:
    await database.db["users"].update_one(
        {"_id": ObjectId(user_id)}, {"$set": {"notices.previous_account_dismissed_at": now}}
    )
