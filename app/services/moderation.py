"""
Moderation: the queue, Keep public and Unpublish (ADMIN-003, ADR-087,
docs/prd-admin.md FR-004..FR-006, FR-011 "Moderation").

A copy records its source in `forked_from.id` (what the fork service writes);
`forked_from.original_id`, the field the model's description names, is read too.

Unpublishing is one operation, whichever route reaches it: the item and every
public copy (3 levels, 200 items) lose `is_public` and carry a removal marker;
private copies are left alone; the item's open reports are resolved; every
removed item is one `content.remove` row in the admin log; each owner is told
by one `content.removed` email at a verified address (ADR-076); the operator
hears it through `notify()`. Every read is bounded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import HTTPException

from app.config.database import db
from app.emails.render import load_strings, resolve_locale
from app.services.admin_actions import record_admin_action
from app.services.email import delivered, send_email
from app.services.notifications import notify
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: A report still waiting for a decision.
OPEN_STATUSES: List[str] = ["pending", "under_review"]
#: What the queue holds and a removal can take.
REMOVABLE_TYPES: List[str] = ["deck", "book"]
#: How deep the copy walk goes, and how many public copies one removal takes (ADR-087).
COPY_LEVELS: int = 3
COPY_LIMIT: int = 200
#: The most copies, public or private, one walk reads.
COPY_READ_LIMIT: int = 1000
#: The most reports one queue item shows, and one decision reads.
REPORTS_PER_ITEM: int = 50
REPORT_READ_LIMIT: int = 5000
#: The fields a copy is read with.
COPY_FIELDS: Dict[str, int] = {"_id": 1, "user_id": 1, "is_public": 1, "name": 1, "title": 1}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collection(content_type: str) -> Any:
    return db[f"{content_type}s"]


def title_of(content_type: str, doc: Optional[Dict[str, Any]]) -> str:
    """A deck's name, a book's title, or "Untitled"."""
    field = "title" if content_type == "book" else "name"
    return str((doc or {}).get(field) or "Untitled")


def _oid(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    return ObjectId(str(value)) if value is not None and ObjectId.is_valid(str(value)) else None


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "not_found"})


# ── Copies ──────────────────────────────────────────────────────────────────


def _copies_of(ids: List[str]) -> Dict[str, Any]:
    return {"$or": [{"forked_from.id": {"$in": ids}}, {"forked_from.original_id": {"$in": ids}}], "deleted_at": None}


async def find_copies(content_type: str, content_id: str) -> List[Dict[str, Any]]:
    """Every live copy, public or private: copies, copies of those, at most 3 levels."""
    found: List[Dict[str, Any]] = []
    seen = {content_id}
    frontier = [content_id]
    for _ in range(COPY_LEVELS):
        room = COPY_READ_LIMIT - len(found)
        if not frontier or room <= 0:
            break
        rows = await _collection(content_type).find(_copies_of(frontier), COPY_FIELDS).to_list(length=room)
        fresh = [row for row in rows if str(row["_id"]) not in seen]
        seen |= {str(row["_id"]) for row in fresh}
        found += fresh
        frontier = [str(row["_id"]) for row in fresh]
    return found


def public_only(copies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The copies a removal takes: the public ones, at most 200."""
    return [copy for copy in copies if copy.get("is_public") is True][:COPY_LIMIT]


# ── The queue ───────────────────────────────────────────────────────────────


def _open_items_pipeline() -> List[Dict[str, Any]]:
    return [
        {"$match": {"content_type": {"$in": REMOVABLE_TYPES}, "status": {"$in": OPEN_STATUSES}}},
        {"$group": {"_id": {"t": "$content_type", "i": "$content_id"}, "order": {"$min": "$created_at"}}},
    ]


def _kept_items_pipeline() -> List[Dict[str, Any]]:
    dismissed = {"$cond": [{"$eq": ["$status", "dismissed"]}, 1, 0]}
    return [
        {"$match": {"content_type": {"$in": REMOVABLE_TYPES}}},
        {"$group": {"_id": {"t": "$content_type", "i": "$content_id"}, "total": {"$sum": 1},
                    "dismissed": {"$sum": dismissed}, "order": {"$max": "$reviewed_at"}}},
        {"$project": {"order": 1, "all_dismissed": {"$eq": ["$total", "$dismissed"]}}},
        {"$match": {"all_dismissed": True}},
    ]


#: A removed item, not a copy taken with it.
REMOVED_QUERY: Dict[str, Any] = {"removal": {"$exists": True}, "removal.copy_of": {"$exists": False}}


async def _count(pipeline: List[Dict[str, Any]]) -> int:
    rows = await db["content_reports"].aggregate(pipeline + [{"$count": "n"}]).to_list(length=1)
    return int(rows[0]["n"]) if rows else 0


async def queue_totals() -> Dict[str, int]:
    removed = 0
    for content_type in REMOVABLE_TYPES:
        removed += await _collection(content_type).count_documents(REMOVED_QUERY)
    return {"to_review": await _count(_open_items_pipeline()), "kept": await _count(_kept_items_pipeline()), "removed": removed}


async def _report_keys(pipeline: List[Dict[str, Any]], direction: int, limit: int) -> List[Tuple[str, str]]:
    rows = await db["content_reports"].aggregate(
        pipeline + [{"$sort": {"order": direction}}, {"$limit": limit}]
    ).to_list(length=limit)
    return [(row["_id"]["t"], row["_id"]["i"]) for row in rows]


async def _removed_keys(limit: int) -> List[Tuple[str, str]]:
    rows: List[Tuple[datetime, str, str]] = []
    for content_type in REMOVABLE_TYPES:
        docs = await _collection(content_type).find(REMOVED_QUERY, {"removal.removed_at": 1}).sort(
            "removal.removed_at", -1).limit(limit).to_list(length=limit)
        rows += [((doc.get("removal") or {}).get("removed_at") or datetime.min, content_type, str(doc["_id"])) for doc in docs]
    rows.sort(key=lambda row: row[0].replace(tzinfo=None), reverse=True)
    return [(content_type, content_id) for _, content_type, content_id in rows[:limit]]


async def queue_keys(state: str, limit: int) -> List[Tuple[str, str]]:
    """(content_type, content_id) for one state of the queue, in its order."""
    if state == "kept":
        return await _report_keys(_kept_items_pipeline(), -1, limit)
    if state == "removed":
        return await _removed_keys(limit)
    return await _report_keys(_open_items_pipeline(), 1, limit)


#: Which reports each state shows on its items.
STATE_REPORTS: Dict[str, List[str]] = {"to_review": OPEN_STATUSES, "kept": ["dismissed"], "removed": ["resolved"]}


async def _item_reports(content_type: str, content_id: str, statuses: List[str]) -> Tuple[List[Dict[str, Any]], int]:
    query = {"content_type": content_type, "content_id": content_id, "status": {"$in": statuses}}
    rows = await db["content_reports"].find(query).sort("created_at", 1).limit(REPORTS_PER_ITEM).to_list(length=REPORTS_PER_ITEM)
    count = await db["content_reports"].count_documents(query)
    reports = [{"id": str(r["_id"]), "reason": r.get("reason"), "note": r.get("description"), "created_at": r.get("created_at")}
               for r in rows]
    return reports, count


async def _size(content_type: str, doc: Dict[str, Any]) -> int:
    """Cards for a deck, pages for a book, 0 when unknown."""
    if content_type == "book":
        return int(doc.get("page_count") or 0)
    deck_id = doc["_id"]
    return await db["cards"].count_documents({"deck_id": {"$in": [deck_id, str(deck_id)]}, "deleted_at": None})


async def _username(user_id: Any) -> Optional[str]:
    oid = _oid(user_id)
    owner = await db["users"].find_one({"_id": oid}, {"username": 1}) if oid else None
    return (owner or {}).get("username")


def _removal_view(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    marker = doc.get("removal")
    if not isinstance(marker, dict):
        return None
    return {"removed_at": marker.get("removed_at"), "reason": marker.get("reason"), "note": marker.get("note")}


async def queue_item(content_type: str, content_id: str, state: str) -> Dict[str, Any]:
    """One item of the queue, with its reports for that state (never the reporter)."""
    oid = _oid(content_id)
    doc = (await _collection(content_type).find_one({"_id": oid}) if oid else None) or {"_id": oid}
    reports, count = await _item_reports(content_type, content_id, STATE_REPORTS[state])
    copies = await find_copies(content_type, content_id)
    return {
        "content_type": content_type, "content_id": content_id, "title": title_of(content_type, doc),
        "owner_username": await _username(doc.get("user_id")), "is_public": doc.get("is_public") is True,
        "public_since": doc.get("published_at"), "size": await _size(content_type, doc) if oid else 0,
        "learners": len({str(copy.get("user_id")) for copy in copies}), "public_copies": len(public_only(copies)),
        "reports": reports, "report_count": count,
        "oldest_report_at": reports[0]["created_at"] if reports else None, "removal": _removal_view(doc),
    }


async def moderation_queue(state: str, limit: int) -> Dict[str, Any]:
    """The Moderation tab: one state's items, in order, and the totals of all three."""
    items = []
    for content_type, content_id in await queue_keys(state, limit):
        item = await queue_item(content_type, content_id, state)
        if state == "kept" and item["removal"]:
            continue  # removed later without a pending report: it belongs to `removed`
        items.append(item)
    return {"items": items, "totals": await queue_totals()}


# ── Keep public ─────────────────────────────────────────────────────────────


async def _open_report_ids(content_type: str, content_id: str) -> List[ObjectId]:
    query = {"content_type": content_type, "content_id": content_id, "status": {"$in": OPEN_STATUSES}}
    rows = await db["content_reports"].find(query, {"_id": 1}).sort("created_at", 1).to_list(length=REPORT_READ_LIMIT)
    return [row["_id"] for row in rows]


async def _close_reports(ids: List[ObjectId], status: str, action: str, actor_id: str, note: Optional[str], now: datetime) -> None:
    changes = {"status": status, "reviewed_by": actor_id, "reviewed_at": now, "action_taken": action, "updated_at": now}
    if note:
        changes["resolution_notes"] = note
    await db["content_reports"].update_many({"_id": {"$in": ids}}, {"$set": changes})


async def keep_public(content_type: str, content_id: str, actor_id: str, note: Optional[str] = None) -> int:
    """Dismiss every open report on the item; one `report.review` row. Answers how many."""
    ids = await _open_report_ids(content_type, content_id)
    if not ids:
        return 0
    await _close_reports(ids, "dismissed", "kept", actor_id, note, _now())
    oid = _oid(content_id)
    doc = await _collection(content_type).find_one({"_id": oid}) if oid else None
    await record_admin_action(
        actor_id, "report.review", content_type, content_id, target_title=title_of(content_type, doc),
        details={"decision": "kept", "report_ids": [str(i) for i in ids]},
    )
    return len(ids)


# ── Unpublish ───────────────────────────────────────────────────────────────


async def _load_removable(content_type: str, content_id: str) -> Dict[str, Any]:
    doc = await _collection(content_type).find_one({"_id": ObjectId(content_id)})
    if doc is None:
        raise _not_found()
    if doc.get("removal"):
        raise HTTPException(status_code=409, detail={"code": "already_removed"})
    return doc


async def _mark_item(content_type: str, oid: ObjectId, marker: Dict[str, Any], now: datetime) -> None:
    """Set the marker on the item, once: a concurrent removal that got there first is a 409."""
    result = await _collection(content_type).update_one(
        {"_id": oid, "removal": {"$exists": False}},
        {"$set": {"is_public": False, "removal": marker, "updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail={"code": "already_removed"})


async def _mark_copies(content_type: str, copies: List[Dict[str, Any]], marker: Dict[str, Any], now: datetime) -> None:
    if copies:
        await _collection(content_type).update_many(
            {"_id": {"$in": [copy["_id"] for copy in copies]}, "removal": {"$exists": False}},
            {"$set": {"is_public": False, "removal": marker, "updated_at": now}},
        )


async def remove_content(
    content_type: str, content_id: str, reason: str, note: Optional[str], actor_id: str, report_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Unpublish the item and its public copies, finally (ADR-087). Answers the RemovalResult shape."""
    item = await _load_removable(content_type, content_id)
    now = _now()
    open_ids = await _open_report_ids(content_type, content_id)
    marker = {"removed_at": now, "removed_by": actor_id, "reason": reason, "note": note or None,
              "report_id": report_id or (str(open_ids[-1]) if open_ids else None)}
    copies = public_only(await find_copies(content_type, content_id))
    await _mark_item(content_type, item["_id"], marker, now)
    await _mark_copies(content_type, copies, {**marker, "copy_of": content_id}, now)
    if open_ids:
        await _close_reports(open_ids, "resolved", "removed", actor_id, note, now)
    outcomes = await _tell_owners(content_type, [item] + copies, reason, note)
    await _log_removal(actor_id, content_type, item, copies, marker, outcomes)
    await notify(*removal_notice(content_type, item, copies, marker, outcomes))
    told = sum(1 for outcome in outcomes.values() if delivered(outcome))
    return {
        "removed": [{"content_type": content_type, "content_id": str(doc["_id"]), "title": title_of(content_type, doc)}
                    for doc in [item] + copies],
        "copies_removed": len(copies), "owners_told": told, "owners_not_told": len(outcomes) - told,
    }


# ── Telling the owners (FR-006) ─────────────────────────────────────────────


def owner_locale(user: Dict[str, Any]) -> Optional[str]:
    """The account's chosen language, as the other account emails read it."""
    return ((user.get("preferences") or {}).get("general") or {}).get("language") or None


def removal_email_values(
    locale: Optional[str], content_type: str, title: str, reason: str, note: Optional[str], is_copy: bool,
) -> Dict[str, str]:
    """The words one `content.removed` email is filled with, in the reader's language."""
    words = load_strings(resolve_locale(locale))["content"]["removed"]
    kind = words["types"][content_type]
    variant = "copy" if is_copy else "own"
    return {
        "title": title,
        "lead": Template(words["lead"][variant]).substitute(type=kind, title=title),
        "reason_line": Template(words["reason_line"][variant]).substitute(reason=words["reasons"][reason]),
        "note_line": Template(words["note_line"]).substitute(note=note) if note else "",
    }


async def _owners(docs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    ids = list({oid for oid in (_oid(doc.get("user_id")) for doc in docs) if oid})
    fields = {"email": 1, "email_verified": 1, "preferences": 1, "deleted_at": 1}
    rows = await db["users"].find({"_id": {"$in": ids}}, fields).to_list(length=len(ids) or 1)
    return {str(row["_id"]): row for row in rows}


async def _tell(owner: Optional[Dict[str, Any]], content_type: str, doc: Dict[str, Any], reason: str,
                note: Optional[str], is_copy: bool) -> str:
    """Send one owner their email; answer the outcome. Only a verified address is written to (ADR-076)."""
    if owner is None or owner.get("deleted_at"):
        return "no_account"
    address = (owner.get("email") or "").strip()
    if owner.get("email_verified") is not True or not address:
        return "unverified"
    locale = owner_locale(owner)
    values = removal_email_values(locale, content_type, title_of(content_type, doc), reason, note, is_copy)
    try:
        return await send_email("content.removed", address, locale, values)
    except Exception as exc:  # noqa: BLE001 - the removal is done; a failed email is recorded, never raised
        logger.error("moderation: the removal email could not be sent: %s", type(exc).__name__)
        return "failed"


async def _tell_owners(content_type: str, docs: List[Dict[str, Any]], reason: str, note: Optional[str]) -> Dict[str, str]:
    """One email per removed item, the first being the item itself. Answers outcome by content id."""
    owners = await _owners(docs)
    outcomes: Dict[str, str] = {}
    for index, doc in enumerate(docs):
        owner = owners.get(str(doc.get("user_id")))
        outcomes[str(doc["_id"])] = await _tell(owner, content_type, doc, reason, note, is_copy=index > 0)
    return outcomes


def told_detail(outcome: str) -> Dict[str, str]:
    """`owner_told` when the email left, `owner_not_told` with the reason otherwise."""
    return {"owner_told": outcome} if delivered(outcome) else {"owner_not_told": outcome}


async def _log_removal(actor_id: str, content_type: str, item: Dict[str, Any], copies: List[Dict[str, Any]],
                       marker: Dict[str, Any], outcomes: Dict[str, str]) -> None:
    item_id = str(item["_id"])
    listed = [{"content_id": str(c["_id"]), "title": title_of(content_type, c), "owner_id": str(c.get("user_id"))} for c in copies]
    shared = {"note": marker["note"], "report_id": marker["report_id"]}
    await record_admin_action(actor_id, "content.remove", content_type, item_id, title_of(content_type, item), marker["reason"],
                              {**shared, "copies": listed, **told_detail(outcomes[item_id])})
    for copy in copies:
        copy_id = str(copy["_id"])
        await record_admin_action(actor_id, "content.remove", content_type, copy_id, title_of(content_type, copy), marker["reason"],
                                  {**shared, "copy_of": item_id, **told_detail(outcomes[copy_id])})


def removal_notice(content_type: str, item: Dict[str, Any], copies: List[Dict[str, Any]], marker: Dict[str, Any],
                   outcomes: Dict[str, str]) -> Tuple[str, str]:
    """Subject and body of the operator's notice."""
    title = title_of(content_type, item)
    told = sum(1 for outcome in outcomes.values() if delivered(outcome))
    lines = [
        f"{content_type.capitalize()} {item['_id']} — {title}",
        f"Reason: {marker['reason']}",
        f"Note: {marker['note'] or '—'}",
        f"Removed by: {marker['removed_by']}",
        f"Report: {marker['report_id'] or '—'}",
        f"Public copies removed: {len(copies)}",
        *[f"  {copy['_id']} — {title_of(content_type, copy)}" for copy in copies[:20]],
        f"Owners told: {told} of {len(outcomes)}",
    ]
    return f"[Nowry] Removed: {title}", "\n".join(lines)
