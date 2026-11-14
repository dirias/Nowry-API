"""
The Overview (ADMIN-007c, docs/prd-admin.md FR-011 "Overview", board A).

What needs the admin (open reports, new bugs, invitations ending within three
days, people waiting), today's email meters for invitations and waitlist
confirmations against their share of the day's budget (ADR-078 point 9), and
the last seven days of the admin log. Every count stops at `COUNT_CAP`; every
read is bounded.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.config.database import (
    admin_actions_collection,
    bugs_collection,
    db,
    email_sends_collection,
    invites_collection,
    waitlist_collection,
)
from app.config.invites import STATUS_OPEN
from app.emails.budget import ceiling, parse_budget
from app.models.Bug import stored_statuses
from app.services import admin_log
from app.services.email_ledger import utc_day
from app.services.moderation import OPEN_STATUSES, REMOVABLE_TYPES
from app.services.waitlist import WAITING

content_reports_collection = db["content_reports"]

#: The most any Overview count reads before it stops.
COUNT_CAP: int = 10_000
#: An invitation "ends soon" within this window.
ENDING_WITHIN = timedelta(days=3)
RECENT_WINDOW = timedelta(days=7)
RECENT_LIMIT = 8
#: How many reasons and cohorts the Overview names.
TOP_NAMES = 2


def next_utc_midnight(now: datetime) -> datetime:
    """When today's email allowance starts again."""
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return start + timedelta(days=1)


async def _count(collection: Any, query: Dict[str, Any]) -> int:
    return int(await collection.count_documents(query, limit=COUNT_CAP))


async def _most_common(collection: Any, query: Dict[str, Any], field: str) -> List[str]:
    """The `TOP_NAMES` most common values of a field among the matching rows, ties by name."""
    rows = await collection.aggregate([
        {"$match": query},
        {"$limit": COUNT_CAP},
        {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
        {"$match": {"_id": {"$ne": None}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": TOP_NAMES},
    ]).to_list(length=TOP_NAMES)
    return [str(row["_id"]) for row in rows]


async def reports_to_review() -> Dict[str, Any]:
    query = {"content_type": {"$in": REMOVABLE_TYPES}, "status": {"$in": OPEN_STATUSES}}
    oldest = await content_reports_collection.find(query, {"created_at": 1}).sort("created_at", 1).limit(1).to_list(length=1)
    return {
        "count": await _count(content_reports_collection, query),
        "oldest_at": admin_log.as_utc(oldest[0]["created_at"]) if oldest and oldest[0].get("created_at") else None,
        "reasons": await _most_common(content_reports_collection, query, "reason"),
    }


async def new_bugs() -> Dict[str, int]:
    query = {"status": {"$in": stored_statuses("new")}}
    return {"count": await _count(bugs_collection, query), "critical": await _count(bugs_collection, {**query, "severity": "critical"})}


async def invitations_ending(now: datetime) -> Dict[str, Any]:
    query = {"status": STATUS_OPEN, "expires_at": {"$gt": now, "$lte": now + ENDING_WITHIN}}
    return {"count": await _count(invites_collection, query), "cohorts": await _most_common(invites_collection, query, "cohort")}


async def _last_batch_at() -> Optional[datetime]:
    rows = await admin_actions_collection.find({"action": "invite.send"}, {"created_at": 1}).sort(
        "created_at", -1).limit(1).to_list(length=1)
    return rows[0]["created_at"] if rows else None


async def waiting() -> Dict[str, int]:
    query: Dict[str, Any] = {"status": WAITING}
    count = await _count(waitlist_collection, query)
    last_batch = await _last_batch_at()
    since = count if last_batch is None else await _count(waitlist_collection, {**query, "created_at": {"$gt": last_batch}})
    return {"count": count, "since_last_batch": since}


async def _sent_today(email_class: str, now: datetime) -> int:
    return await _count(email_sends_collection, {"day": utc_day(now), "class": email_class, "outcome": "sent"})


async def emails_today(now: datetime) -> Dict[str, Any]:
    budget = parse_budget(os.getenv("EMAIL_DAILY_BUDGET"))
    return {
        "invitations": {"sent": await _sent_today("invitation", now), "share": ceiling("invitation", budget)},
        "confirmations": {"sent": await _sent_today("confirmation", now), "share": ceiling("confirmation", budget)},
        "resets_at": next_utc_midnight(now),
    }


async def overview(caller_id: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the Overview tab shows, in one answer."""
    now = now or datetime.now(timezone.utc)
    return {
        "needs": {
            "reports_to_review": await reports_to_review(),
            "new_bugs": await new_bugs(),
            "invitations_ending": await invitations_ending(now),
            "waiting": await waiting(),
        },
        "emails_today": await emails_today(now),
        "recent_actions": await admin_log.recent_actions(now - RECENT_WINDOW, RECENT_LIMIT, caller_id),
    }
