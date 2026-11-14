"""
The waitlist as the operator reads it (WAIT-002, docs/prd-admin.md FR-012, board E).

Rows oldest first, filtered by state, language and source, never more than
`LIST_MAX`. The counts (per state, per language, the most common sources) are
over every row, read by bounded aggregations. A row's language is the
two-letter code its letters go in (`resolve_locale`): `es-MX` is `es`, and a
code outside the five is English. The leave token is never read.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config.database import waitlist_collection
from app.emails.render import resolve_locale

STATES = ("waiting", "invited", "joined", "left")
LIST_MAX = 200
#: How many sources the filter offers.
SOURCES_SHOWN = 20
#: The most distinct stored languages read when counting; the form stores at most five characters.
LOCALE_GROUPS_MAX = 500
_FIELDS = {"email": 1, "locale": 1, "source": 1, "status": 1, "created_at": 1, "updated_at": 1, "invited_at": 1, "joined_at": 1}


async def _groups(pipeline: List[Dict[str, Any]], bound: int, collection: Any) -> List[Dict[str, Any]]:
    return await collection.aggregate(pipeline).to_list(length=bound)


async def count_states(collection: Any) -> Dict[str, int]:
    """Every row by state."""
    groups = await _groups([{"$group": {"_id": "$status", "n": {"$sum": 1}}}], 50, collection)
    found = {group["_id"]: group["n"] for group in groups}
    return {state: int(found.get(state, 0)) for state in STATES}


async def stored_locales(collection: Any) -> Dict[str, int]:
    """{stored language: rows}, rows without one left out."""
    pipeline = [{"$match": {"locale": {"$ne": None}}}, {"$group": {"_id": "$locale", "n": {"$sum": 1}}}]
    return {group["_id"]: int(group["n"]) for group in await _groups(pipeline, LOCALE_GROUPS_MAX, collection)}


def resolved_counts(stored: Dict[str, int]) -> Dict[str, int]:
    """The stored languages summed by the code they read as."""
    counts: Dict[str, int] = {}
    for raw, n in stored.items():
        code = resolve_locale(raw)
        counts[code] = counts.get(code, 0) + n
    return counts


async def common_sources(collection: Any) -> List[str]:
    """The most common sources, most common first (ties by name)."""
    pipeline = [
        {"$match": {"source": {"$ne": None}}},
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": SOURCES_SHOWN},
    ]
    return [group["_id"] for group in await _groups(pipeline, SOURCES_SHOWN, collection)]


def row_filter(state: Optional[str], locale: Optional[str], source: Optional[str], stored: Dict[str, int]) -> Dict[str, Any]:
    """The query for the rows asked for; a language matches every stored value that reads as it."""
    query: Dict[str, Any] = {"status": state} if state else {"status": {"$in": list(STATES)}}
    if locale:
        query["locale"] = {"$in": [raw for raw in stored if resolve_locale(raw) == locale]}
    if source:
        query["source"] = source
    return query


def admin_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """A stored row as the operator reads it."""
    return {
        "id": str(row["_id"]),
        "email": row["email"],
        "locale": resolve_locale(row["locale"]) if row.get("locale") else None,
        "source": row.get("source"),
        "status": row["status"],
        "created_at": row.get("created_at") or row.get("updated_at"),
        "invited_at": row.get("invited_at"),
        "joined_at": row.get("joined_at"),
    }


async def list_waitlist(
    state: Optional[str], locale: Optional[str], source: Optional[str], limit: int, collection: Any = None
) -> Dict[str, Any]:
    """The rows asked for, oldest first, with the counts over every row."""
    collection = waitlist_collection if collection is None else collection
    stored = await stored_locales(collection)
    bounded = max(1, min(limit, LIST_MAX))
    cursor = collection.find(row_filter(state, locale, source, stored), _FIELDS).sort([("created_at", 1), ("_id", 1)]).limit(bounded)
    rows = await cursor.to_list(length=bounded)
    return {
        "rows": [admin_row(row) for row in rows],
        "totals": await count_states(collection),
        "locales": resolved_counts(stored),
        "sources": await common_sources(collection),
    }
