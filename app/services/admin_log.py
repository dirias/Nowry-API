"""
Reading the admin log (ADMIN-007c, docs/prd-admin.md FR-011 "Overview" and `/admin/log`).

Rows come newest first, by kind, a page at a time: `before` is an exclusive
cursor on `created_at`. Actors are named by username in one bounded query, the
grant script as "script". Details are cut to a safe subset: never an address
(the title is the only place one appears), a token, an owner id or a copy's list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.config.database import admin_actions_collection, users_collection

#: Which actions each filter of the log shows (the whole fixed list, once each).
KINDS: Dict[str, List[str]] = {
    "invitations": ["invite.create", "invite.send", "invite.resend", "invite.revoke"],
    "moderation": ["report.review", "content.remove", "curation.review"],
    "bugs": ["bug.status"],
    "access": ["admin.grant", "admin.revoke"],
}

#: The details a reader sees; plain values only.
SAFE_DETAIL_KEYS = ("cohort", "outcome", "from", "to", "note_added", "decision", "before", "after")
#: The actor id the grant script writes.
SCRIPT_ACTOR = "script"

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def kind_of(action: str) -> Optional[str]:
    """The log filter an action belongs to."""
    return next((kind for kind, actions in KINDS.items() if action in actions), None)


def _plain(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def safe_details(details: Any) -> Dict[str, Any]:
    """The subset of a row's details any admin may read; `copies` becomes their count."""
    if not isinstance(details, dict):
        return {}
    kept = {key: details[key] for key in SAFE_DETAIL_KEYS if key in details and _plain(details[key])}
    if isinstance(details.get("copies"), list):
        kept["copies"] = len(details["copies"])
    return kept


def as_utc(moment: datetime) -> datetime:
    """Mongo answers naive UTC datetimes; the wire carries them with their zone."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


async def usernames(actor_ids: List[str]) -> Dict[str, Optional[str]]:
    """{actor id: username} for the ids that are accounts, in one bounded query."""
    ids = list({ObjectId(actor) for actor in actor_ids if ObjectId.is_valid(actor)})
    if not ids:
        return {}
    accounts = await users_collection.find({"_id": {"$in": ids}}, {"username": 1}).to_list(length=len(ids))
    return {str(account["_id"]): account.get("username") for account in accounts}


def _actor(actor_id: Any, names: Dict[str, Optional[str]], caller_id: Optional[str]) -> Dict[str, Any]:
    if actor_id == SCRIPT_ACTOR:
        return {"kind": "script", "username": None, "is_you": False}
    actor = str(actor_id)
    return {"kind": "admin", "username": names.get(actor), "is_you": bool(caller_id) and actor == caller_id}


def action_view(row: Dict[str, Any], names: Dict[str, Optional[str]], caller_id: Optional[str]) -> Dict[str, Any]:
    """One stored row as the Overview and the log show it."""
    target_id = row.get("target_id")
    return {
        "id": str(row["_id"]),
        "at": as_utc(row["created_at"]),
        "actor": _actor(row.get("actor_id"), names, caller_id),
        "action": row["action"],
        "target_type": row.get("target_type") or "",
        "target_id": None if target_id is None else str(target_id),
        "target_title": row.get("target_title"),
        "reason": row.get("reason"),
        "details": safe_details(row.get("details")),
    }


def actions_query(kind: Optional[str], before: Optional[datetime] = None, since: Optional[datetime] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {"action": {"$in": KINDS[kind]}} if kind else {}
    window: Dict[str, Any] = {}
    if before is not None:
        window["$lt"] = before
    if since is not None:
        window["$gt"] = since
    if window:
        query["created_at"] = window
    return query


async def _views(rows: List[Dict[str, Any]], caller_id: Optional[str]) -> List[Dict[str, Any]]:
    names = await usernames([str(row.get("actor_id")) for row in rows])
    return [action_view(row, names, caller_id) for row in rows]


async def read_actions(
    kind: Optional[str], before: Optional[datetime], limit: int, caller_id: Optional[str]
) -> Dict[str, Any]:
    """One page of the log, newest first; `next_before` only while older rows exist."""
    rows = await admin_actions_collection.find(actions_query(kind, before)).sort("created_at", -1).limit(
        limit + 1).to_list(length=limit + 1)
    page = page_before_cursor(rows, limit)
    views = await _views(page, caller_id)
    return {"actions": views, "next_before": views[-1]["at"] if len(rows) > limit else None}


def page_before_cursor(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The first `limit` rows, less any that share a time with the first row left out.

    The cursor is exclusive on time, so rows written in the same millisecond
    (a removal and its copies) must start the next page together rather than
    straddle it. A page made only of one instant is answered whole.
    """
    page = rows[:limit]
    if len(rows) <= limit:
        return page
    boundary = rows[limit]["created_at"]
    trimmed = [row for row in page if row["created_at"] != boundary]
    return trimmed or page


async def recent_actions(since: datetime, limit: int, caller_id: Optional[str]) -> List[Dict[str, Any]]:
    """The newest rows after `since`, at most `limit`."""
    rows = await admin_actions_collection.find(actions_query(None, since=since)).sort("created_at", -1).limit(
        limit).to_list(length=limit)
    return await _views(rows, caller_id)
