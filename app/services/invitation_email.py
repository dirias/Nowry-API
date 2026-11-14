"""
The invitation letter (INVITE-008, ADR-077 amendment, docs/prd-admin.md FR-012).

`beta.invitation` is a letter to a visitor (ADR-078 point 1b): personal sender,
invitation class, and the waitlist's leave link with its one-click header
(ADR-080). The leave token lives on the address's waitlist row, so an address
invited without one is given a row first (`status: invited`, `source:
invitation`): leaving then works for it like for anyone on the list, and a
later batch reads it as `left_list`. The link to register exists only in the
email; nothing here returns it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.config.database import invites_collection, waitlist_collection
from app.config.invites import invite_link
from app.emails.render import format_day
from app.services import email as email_service
from app.services import waitlist
from app.utils.logger import get_logger

logger = get_logger(__name__)

INVITATION_KIND = "beta.invitation"
INVITED = "invited"
#: The waitlist `source` of a row made for an address invited from outside the list.
INVITATION_SOURCE = "invitation"


async def waitlist_row_for(address: str, locale: str, collection: Any, now: datetime) -> Dict[str, Any]:
    """The address's waitlist row, made when it has none, so the letter can carry its leave link."""
    await collection.update_one(
        {"email": address},
        {"$setOnInsert": {
            "locale": locale, "source": INVITATION_SOURCE, "status": INVITED, "created_at": now,
            "updated_at": now, "leave_token": waitlist.new_leave_token(),
        }},
        upsert=True,
    )
    return await collection.find_one({"email": address})


async def deliver(row: Dict[str, Any], token: str, waitlist_rows: Any = None, now: Optional[datetime] = None) -> str:
    """Email one invitation row its link. Returns the send outcome; never raises."""
    rows = waitlist_collection if waitlist_rows is None else waitlist_rows
    at = now or datetime.now(timezone.utc)
    try:
        entry = await waitlist_row_for(row["email"], row.get("locale") or "en", rows, at)
        links = waitlist.leave_links(await waitlist._token_of(entry, rows))
        if links is None:
            logger.warning("invitation: no public API address (API_PUBLIC_URL), so no invitation can carry its one-click header")
            return email_service.UNCONFIGURED
        values = {"link": invite_link(token), "expires": format_day(row["expires_at"], row.get("locale")), "leave_link": links["page"]}
        return await email_service.send_email(
            INVITATION_KIND, row["email"], row.get("locale"), values, headers=waitlist.unsubscribe_headers(links["one_click"]),
        )
    except Exception as exc:  # noqa: BLE001 - a failed letter leaves the invitation open, never a 500
        logger.warning("invitation: the letter could not be sent: %s", type(exc).__name__)
        return email_service.FAILED


async def record_send(row: Dict[str, Any], outcome: str, invites: Any = None, waitlist_rows: Any = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Note the outcome on the invitation row, and mark a waiting list row invited once a letter left."""
    target = invites_collection if invites is None else invites
    at = now or datetime.now(timezone.utc)
    if email_service.delivered(outcome):
        change: Dict[str, Any] = {"$set": {"sent_at": at, "send_count": int(row.get("send_count") or 0) + 1, "last_error": None}}
        await (waitlist_collection if waitlist_rows is None else waitlist_rows).update_one(
            {"email": row["email"], "status": waitlist.WAITING}, {"$set": {"status": INVITED, "invited_at": at, "updated_at": at}},
        )
    else:
        change = {"$set": {"last_error": outcome}}
    await target.update_one({"_id": row["_id"]}, change)
    return {**row, **change["$set"]}


async def send_invitation(
    row: Dict[str, Any], token: str, invites: Any = None, waitlist_rows: Any = None, now: Optional[datetime] = None
) -> Tuple[Dict[str, Any], str]:
    """Send the letter for a new invitation row; the row as it now is, and the send outcome."""
    outcome = await deliver(row, token, waitlist_rows, now)
    return await record_send(row, outcome, invites, waitlist_rows, now), outcome
