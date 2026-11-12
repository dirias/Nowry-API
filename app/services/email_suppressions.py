"""
Addresses a visitor email is never sent to again (MAIL-008, ADR-080 point 5).

Resend reports a permanent bounce or a complaint; the address is kept here by
the ledger's hash, never in the clear. Reading never raises: a list that
cannot be read answers "marked", because writing to a person who complained is
worse than one confirmation not sent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.config.database import email_suppressions_collection
from app.services.email_ledger import recipient_hash
from app.utils.logger import get_logger

logger = get_logger(__name__)

BOUNCED = "bounced"
COMPLAINED = "complained"


async def is_suppressed(address: str, collection=None) -> bool:
    try:
        row = await (collection if collection is not None else email_suppressions_collection).find_one({"_id": recipient_hash(address)})
    except Exception as exc:  # noqa: BLE001 - see the module note
        logger.warning("email suppressions: could not be read, nothing is sent: %s", exc)
        return True
    return row is not None


async def suppress(address: str, reason: str, collection=None, now: Optional[datetime] = None) -> None:
    """Mark an address. The first reason is kept; a later report only adds its time."""
    at = now or datetime.now(timezone.utc)
    await (collection if collection is not None else email_suppressions_collection).update_one(
        {"_id": recipient_hash(address)},
        {"$setOnInsert": {"reason": reason, "created_at": at}, "$set": {"updated_at": at}},
        upsert=True,
    )
