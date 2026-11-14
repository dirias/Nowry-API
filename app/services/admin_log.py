"""
Reading the admin log (ADMIN-007c, docs/prd-admin.md FR-011 "Overview" and `/admin/log`).

Rows come newest first (time, then id), by kind, a page at a time: `before`
is the exclusive cursor "<instant>~<id>" a page answered, or a plain instant
(everything before it), so rows written in one instant are never skipped. Actors are named by username in one bounded query, the
grant script as "script". Details are cut to a safe subset: never an address
(the title is the only place one appears), a token, an owner id or a copy's list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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


#: Newest first; rows of one instant by id, so the order is total and a cursor can resume it.
NEWEST_FIRST = [("created_at", -1), ("_id", -1)]
#: Between the instant and the id in a `next_before` cursor.
CURSOR_SEPARATOR = "~"


class BadCursor(ValueError):
    """A `before` that is neither a cursor this API wrote nor an ISO instant."""


def _naive_utc(moment: datetime) -> datetime:
    """Mongo stores naive UTC; compare in the same form (equality between naive and aware never matches)."""
    return moment if moment.tzinfo is None else moment.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_z(moment: datetime) -> str:
    return as_utc(moment).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def encode_cursor(row: Dict[str, Any]) -> str:
    """The opaque `next_before`: "<ISO instant with Z>~<id>" of the last row answered."""
    return f"{_iso_z(row['created_at'])}{CURSOR_SEPARATOR}{row['_id']}"


def _parse_instant(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadCursor(text) from exc


def decode_cursor(before: Optional[str]) -> Optional[Tuple[datetime, Optional[ObjectId]]]:
    """(instant, id) from a cursor, or (instant, None) from a plain ISO datetime; None when absent."""
    if before is None or not before.strip():
        return None
    parts = before.split(CURSOR_SEPARATOR)
    if len(parts) > 2 or (len(parts) == 2 and not ObjectId.is_valid(parts[1])):
        raise BadCursor(before)
    return _naive_utc(_parse_instant(parts[0])), ObjectId(parts[1]) if len(parts) == 2 else None


def _before(cursor: Tuple[datetime, Optional[ObjectId]]) -> Dict[str, Any]:
    instant, row_id = cursor
    if row_id is None:
        return {"created_at": {"$lt": instant}}
    return {"$or": [{"created_at": {"$lt": instant}}, {"created_at": instant, "_id": {"$lt": row_id}}]}


def actions_query(
    kind: Optional[str], cursor: Optional[Tuple[datetime, Optional[ObjectId]]] = None, since: Optional[datetime] = None
) -> Dict[str, Any]:
    clauses: List[Dict[str, Any]] = []
    if kind:
        clauses.append({"action": {"$in": KINDS[kind]}})
    if cursor is not None:
        clauses.append(_before(cursor))
    if since is not None:
        clauses.append({"created_at": {"$gt": _naive_utc(since)}})
    return clauses[0] if len(clauses) == 1 else ({"$and": clauses} if clauses else {})


async def _views(rows: List[Dict[str, Any]], caller_id: Optional[str]) -> List[Dict[str, Any]]:
    names = await usernames([str(row.get("actor_id")) for row in rows])
    return [action_view(row, names, caller_id) for row in rows]


async def read_actions(kind: Optional[str], before: Optional[str], limit: int, caller_id: Optional[str]) -> Dict[str, Any]:
    """One page of the log, newest first; `next_before` only while older rows exist. Raises BadCursor."""
    query = actions_query(kind, decode_cursor(before))
    rows = await admin_actions_collection.find(query).sort(NEWEST_FIRST).limit(limit + 1).to_list(length=limit + 1)
    page = rows[:limit]
    return {"actions": await _views(page, caller_id), "next_before": encode_cursor(page[-1]) if len(rows) > limit else None}


async def recent_actions(since: datetime, limit: int, caller_id: Optional[str]) -> List[Dict[str, Any]]:
    """The newest rows after `since`, at most `limit`."""
    rows = await admin_actions_collection.find(actions_query(None, since=since)).sort(NEWEST_FIRST).limit(
        limit).to_list(length=limit)
    return await _views(rows, caller_id)
