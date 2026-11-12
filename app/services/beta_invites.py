"""
What the beta gate reads from the database (ADR-077).

The rules live in `app/config/beta.py` and take these as arguments, so they are
tested without Mongo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ReturnDocument

from app.config import invites as rules
from app.config.database import invites_collection, users_collection

#: The most rows one listing returns.
LIST_MAX: int = 200


async def count_code_uses(code: str, ceiling: int) -> int:
    """How many accounts carry this cohort code, counted no further than `ceiling`.

    Deleted accounts count: the code made them. The limit bounds the read, and
    the sparse index on `beta.invite_code` serves it.
    """
    return await users_collection.count_documents({"beta.invite_code": code}, limit=max(ceiling, 1))


async def create_invite(
    email: str, cohort: str, created_by: str, days: Optional[int] = None, note: Optional[str] = None,
    collection=None, now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], str]:
    """Make an invitation for one address and return its row and its token.

    An open invitation for the same address is revoked first, so one address
    never holds two working links. The token leaves here once and is not kept.
    """
    collection = invites_collection if collection is None else collection
    at = now or datetime.now(timezone.utc)
    await collection.update_many(
        {"email": rules.normalize_email(email), "status": rules.STATUS_OPEN},
        {"$set": {"status": rules.STATUS_REVOKED, "revoked_at": at, "revoked_by": created_by}},
    )
    token = rules.make_token()
    row = rules.new_invite_document(email, cohort, token, created_by, at, days, note)
    result = await collection.insert_one(row)
    row["_id"] = result.inserted_id
    return row, token


async def list_invites(
    state: Optional[str] = None, cohort: Optional[str] = None, limit: int = 50, collection=None, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """The newest invitations first, filtered, never more than `LIST_MAX`."""
    collection = invites_collection if collection is None else collection
    query = rules.state_filter(state, now or datetime.now(timezone.utc))
    if cohort:
        query["cohort"] = cohort
    bounded = max(1, min(limit, LIST_MAX))
    return await collection.find(query).sort("created_at", -1).limit(bounded).to_list(length=bounded)


async def count_invites_by_cohort(collection=None, now: Optional[datetime] = None) -> Dict[str, Dict[str, int]]:
    """{cohort: {open, used, revoked, expired}} over every invitation."""
    collection = invites_collection if collection is None else collection
    at = now or datetime.now(timezone.utc)
    state = {
        "$cond": [
            {"$and": [{"$eq": ["$status", rules.STATUS_OPEN]}, {"$lte": ["$expires_at", at]}]},
            rules.STATE_EXPIRED,
            "$status",
        ]
    }
    groups = await collection.aggregate(
        [{"$group": {"_id": {"cohort": "$cohort", "state": state}, "n": {"$sum": 1}}}]
    ).to_list(length=2000)
    counts: Dict[str, Dict[str, int]] = {}
    for group in groups:
        per_cohort = counts.setdefault(group["_id"]["cohort"], {name: 0 for name in rules.STATES})
        per_cohort[group["_id"]["state"]] = group["n"]
    return counts


async def revoke_invite(invite_id: str, revoked_by: str, collection=None, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Revoke an open invitation and return the row as it now is; None when there is no such row.

    A used or already revoked invitation is returned unchanged: what happened stays on record.
    """
    collection = invites_collection if collection is None else collection
    if not ObjectId.is_valid(invite_id):
        return None
    oid = ObjectId(invite_id)
    at = now or datetime.now(timezone.utc)
    revoked = await collection.find_one_and_update(
        {"_id": oid, "status": rules.STATUS_OPEN},
        {"$set": {"status": rules.STATUS_REVOKED, "revoked_at": at, "revoked_by": revoked_by}},
        return_document=ReturnDocument.AFTER,
    )
    return revoked or await collection.find_one({"_id": oid})
