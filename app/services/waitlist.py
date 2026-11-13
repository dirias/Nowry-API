"""
The waitlist's rows and what happens to them (ADR-038, ADR-080; FR-004, FR-026, FR-027).

A row is one address. It is `waiting` or `left`, may be marked `suppressed`
after a bounce or a complaint, carries one leave token for every email it is
ever sent, and records when its one confirmation left.

    join               the form: upsert by address; tells whether it is new
    send_confirmation  once per address, ever, claimed on the row first
    leave_hint         what the leave page shows before anything changes
    leave              the page's key and a mail provider's one-click request
    mark_suppressed    Resend said this address bounced or complained
    send_account_letter  the form met an address that has an account (ADR-084)
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from pymongo import ReturnDocument

from app.models.Beta import WaitlistCreate
from app.services import email as email_service
from app.services import email_suppressions
from app.utils.keyed_rate_limit import claim_rolling
from app.utils.logger import get_logger

logger = get_logger(__name__)

WAITING = "waiting"
LEFT = "left"
CONFIRMATION_KIND = "waitlist.confirm"
ACCOUNT_KIND = "waitlist.account"
#: One account letter per address in this many seconds (ADR-084 point 5).
ACCOUNT_LETTER_WINDOW = 24 * 3600

#: A claim older than this was left by a worker that died mid-send, and may be taken again.
CLAIM_TTL = timedelta(minutes=10)

LEAVE_PAGE_PATH = "/waitlist/leave/"
LEAVE_API_PATH = "/beta/waitlist/leave/"


def new_leave_token() -> str:
    return secrets.token_urlsafe(32)


def looks_like_token(token: str) -> bool:
    """Cheap shape check, so a pasted novel never reaches the database."""
    return 20 <= len(token) <= 64 and all(ch.isalnum() or ch in "-_" for ch in token)


def mask_email(address: str) -> str:
    """`mika@example.com` → `m•••@example.com`: enough to recognise, not enough to read."""
    local, at, domain = address.partition("@")
    return f"{local[:1]}•••{at}{domain}" if at else "•••"


def waitlist_document(payload: WaitlistCreate, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The fields an upsert sets: the form's, lowercased email, a status, times."""
    at = now or datetime.now(timezone.utc)
    return {
        "email": payload.email.lower(),
        "locale": payload.locale,
        "source": payload.source,
        "status": WAITING,
        "updated_at": at,
    }


async def join(payload: WaitlistCreate, collection, now: Optional[datetime] = None) -> Tuple[Dict[str, Any], bool]:
    """Upsert by email so a second submission updates rather than duplicates.

    Returns the row's fields and whether the email is new. The update hands back
    the row as it was before the write: none means this was the first time. A
    row that had left is `waiting` again, and is not new.
    """
    doc = waitlist_document(payload, now)
    before = await collection.find_one_and_update(
        {"email": doc["email"]},
        {"$set": doc, "$unset": {"left_at": ""}, "$setOnInsert": {"created_at": doc["updated_at"], "leave_token": new_leave_token()}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    return doc, before is None


def api_public_url() -> str:
    """Where a mail provider can reach the API: `API_PUBLIC_URL`, else Railway's own domain."""
    explicit = (os.getenv("API_PUBLIC_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    return f"https://{domain}" if domain else ""


def leave_links(token: str) -> Optional[Dict[str, str]]:
    """The page a person opens and the address a mail provider posts to, or None when the API has no public address."""
    api = api_public_url()
    if not api:
        return None
    return {"page": f"{email_service.frontend_url()}{LEAVE_PAGE_PATH}{token}", "one_click": f"{api}{LEAVE_API_PATH}{token}"}


def unsubscribe_headers(one_click_url: str) -> Dict[str, str]:
    """RFC 8058: the address, and the promise that a bare POST to it is enough."""
    return {"List-Unsubscribe": f"<{one_click_url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


async def _claim_confirmation(address: str, collection, now: datetime) -> Optional[Dict[str, Any]]:
    """Take the row's one confirmation, or None when it is not owed: sent, left, marked, or being sent."""
    return await collection.find_one_and_update(
        {
            "email": address,
            "status": WAITING,
            "confirmation_sent_at": {"$exists": False},
            "suppressed": {"$exists": False},
            "$or": [{"confirmation_claimed_at": {"$exists": False}}, {"confirmation_claimed_at": {"$lt": now - CLAIM_TTL}}],
        },
        {"$set": {"confirmation_claimed_at": now}},
        return_document=ReturnDocument.AFTER,
    )


async def _token_of(row: Dict[str, Any], collection) -> str:
    """The row's leave token; a row older than ADR-080 is given one now."""
    if row.get("leave_token"):
        return row["leave_token"]
    token = new_leave_token()
    await collection.update_one({"_id": row["_id"], "leave_token": {"$exists": False}}, {"$set": {"leave_token": token}})
    fresh = await collection.find_one({"_id": row["_id"]}, {"leave_token": 1})
    return (fresh or {}).get("leave_token") or token


async def send_confirmation(address: str, collection, now: Optional[datetime] = None) -> str:
    """Send the one confirmation this address is owed. Returns the outcome; never raises.

    `skipped` when none is owed. Anything but `sent` releases the claim, so a
    later submission can try again (ADR-080 point 6).
    """
    at = now or datetime.now(timezone.utc)
    try:
        row = await _claim_confirmation(address.lower(), collection, at)
        if row is None:
            return "skipped"
        links = leave_links(await _token_of(row, collection))
        if links is None:
            logger.warning("waitlist: no public API address (API_PUBLIC_URL), so no confirmation can carry its one-click header")
            outcome = email_service.UNCONFIGURED
        else:
            outcome = await email_service.send_email(
                CONFIRMATION_KIND, row["email"], row.get("locale"),
                {"leave_link": links["page"]}, headers=unsubscribe_headers(links["one_click"]),
            )
        if outcome == email_service.SENT:
            await collection.update_one({"_id": row["_id"]}, {"$set": {"confirmation_sent_at": at}, "$unset": {"confirmation_claimed_at": ""}})
        else:
            await collection.update_one({"_id": row["_id"]}, {"$unset": {"confirmation_claimed_at": ""}})
        return outcome
    except Exception as exc:  # noqa: BLE001 - runs after the answer; nothing is waiting for it
        logger.warning("waitlist: the confirmation could not be sent: %s", exc)
        return email_service.FAILED


async def leave_hint(token: str, collection) -> Optional[Dict[str, Optional[str]]]:
    """What the leave page shows for a token: a masked address and the row's language. None for any other token."""
    if not looks_like_token(token):
        return None
    row = await collection.find_one({"leave_token": token}, {"email": 1, "locale": 1})
    if not row:
        return None
    return {"email_hint": mask_email(row["email"]), "locale": row.get("locale")}


async def leave(token: str, collection, now: Optional[datetime] = None) -> bool:
    """Mark the token's row `left`. True when a row changed; the caller answers the same either way."""
    if not looks_like_token(token):
        return False
    at = now or datetime.now(timezone.utc)
    result = await collection.update_one(
        {"leave_token": token, "status": {"$ne": LEFT}},
        {"$set": {"status": LEFT, "left_at": at, "updated_at": at}},
    )
    return result.modified_count > 0


async def mark_suppressed(address: str, reason: str, collection, suppressions=None, now: Optional[datetime] = None) -> None:
    """Resend said this address bounced for good or complained: never a visitor email again."""
    at = now or datetime.now(timezone.utc)
    await email_suppressions.suppress(address, reason, suppressions, at)
    await collection.update_one({"email": address.strip().lower()}, {"$set": {"suppressed": reason, "suppressed_at": at}})


def _address_key(address: str) -> str:
    """The limit is counted on a hash, so the counter never holds an address."""
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()[:32]


async def send_account_letter(address: str, locale: Optional[str]) -> str:
    """Tell an address that already has an account so, at most once a day (ADR-084). Never raises.

    `limited` inside the 24 hours. A suppressed address and a spent
    confirmation share are refused by `send_email` itself.
    """
    try:
        if not await claim_rolling("waitlist_account", _address_key(address), ACCOUNT_LETTER_WINDOW):
            return "limited"
        link = f"{email_service.frontend_url()}/login"
        return await email_service.send_email(ACCOUNT_KIND, address.strip().lower(), locale, {"link": link})
    except Exception as exc:  # noqa: BLE001 - runs after the answer; nothing is waiting for it
        logger.warning("waitlist: the account letter could not be sent: %s", type(exc).__name__)
        return email_service.FAILED
