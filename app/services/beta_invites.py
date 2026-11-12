"""
What the beta gate reads from the database (ADR-077).

The rules live in `app/config/beta.py` and take these as arguments, so they are
tested without Mongo.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument

from app.config import invites as rules
from app.config.beta import BETA_INVITE_REQUIRED_CODE, beta_config, gate_new_account
from app.config.database import invites_collection, users_collection

logger = logging.getLogger(__name__)

#: The 403 a sign-up with a personal invitation gets until its address is verified (ADR-077).
INVITE_VERIFY_EMAIL_CODE: str = "invite_verify_email"

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


# ── Spending an invitation at account creation (INVITE-003) ──────────────────


async def spend_personal_invite(token: str, token_data: Dict[str, Any], collection=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Spend a personal invitation for the signed-in address and return the `beta` stamp.

    Two answers, and neither tells one address from another. Until Firebase has
    verified the email the answer is `invite_verify_email` whatever the token:
    nothing is looked up. Once verified, the one atomic claim either succeeds
    or the answer is the refusal a missing code gets, whether the token is
    unknown, used, expired, revoked or made for someone else.
    """
    if not token_data.get("email_verified"):
        raise HTTPException(status_code=403, detail={"code": INVITE_VERIFY_EMAIL_CODE})
    collection = invites_collection if collection is None else collection
    at = now or datetime.now(timezone.utc)
    uid: str = token_data.get("firebase_uid") or ""
    row = await collection.find_one_and_update(
        rules.claim_query(token, token_data.get("email") or "", uid, at),
        rules.claim_update(uid, at),
        return_document=ReturnDocument.AFTER,
    )
    if row is None:
        raise HTTPException(status_code=403, detail={"code": BETA_INVITE_REQUIRED_CODE})
    return {"cohort": row["cohort"], "invite_id": str(row["_id"]), "joined_at": at}


async def gate_account_creation(invite_header: Optional[str], token_data: Dict[str, Any], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The one check account creation runs: a personal invitation or a cohort code.

    Returns the `beta` stamp for the new document, None when the gate is off,
    or raises the refusal. Never consulted for an account that already exists.
    """
    if not beta_config()["invite_required"]:
        return None
    if rules.is_personal_token(invite_header):
        return await spend_personal_invite(invite_header, token_data, now=now)
    return await gate_new_account(invite_header, count_code_uses, now)


async def settle_personal_invite(stamp: Optional[Dict[str, Any]], user_id: str, firebase_uid: str, collection=None) -> None:
    """Tie a spent invitation to the account it made. A cohort stamp has nothing to tie."""
    if not stamp or not stamp.get("invite_id"):
        return
    collection = invites_collection if collection is None else collection
    await collection.update_one(
        {"_id": ObjectId(stamp["invite_id"])},
        {"$set": {"status": rules.STATUS_USED, "used_by": user_id, "used_by_uid": firebase_uid}},
    )


async def release_personal_invite(stamp: Optional[Dict[str, Any]], firebase_uid: str, collection=None) -> None:
    """Give an invitation back when the account it was spent on was never written. Never raises."""
    if not stamp or not stamp.get("invite_id"):
        return
    collection = invites_collection if collection is None else collection
    try:
        await collection.update_one(rules.release_query(ObjectId(stamp["invite_id"]), firebase_uid), rules.RELEASE_UPDATE)
    except Exception:
        logger.exception("beta: a spent invitation could not be released after a failed sign-up")


async def personal_invite_usable(token: str, collection=None, now: Optional[datetime] = None) -> bool:
    """Whether a token could still make an account. Says nothing of why not, or for whom."""
    collection = invites_collection if collection is None else collection
    row = await collection.find_one({"token_hash": rules.hash_token(token)})
    return rules.is_usable(row, now or datetime.now(timezone.utc))
