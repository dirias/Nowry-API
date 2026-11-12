"""
The send ledger (MAIL-004, ADR-078 point 9): how many emails went out today,
and one row per attempt. A row holds the kind, the class, the UTC day, the
outcome, the language and a hash of the recipient. Never the address, never a link.

Neither function raises. A ledger that cannot be read counts as zero and a row
that cannot be written is logged: email must not stop because bookkeeping did.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from app.config.database import email_sends_collection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def recipient_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()


def ledger_row(kind: str, email_class: str, outcome: str, locale: str, to: str, now: datetime) -> dict:
    """What one attempt leaves behind."""
    return {
        "kind": kind, "class": email_class, "outcome": outcome, "locale": locale,
        "recipient_hash": recipient_hash(to), "day": utc_day(now), "created_at": now,
    }


async def sent_today() -> int:
    """Sends accepted today: our own rows, or Resend's count when that is higher."""
    day = utc_day()
    try:
        own = await email_sends_collection.count_documents({"day": day, "outcome": "sent"})
        quota = await email_sends_collection.find_one({"_id": f"quota:{day}"})
        return max(int(own), int((quota or {}).get("resend_count", 0)))
    except Exception as exc:  # noqa: BLE001 - see the module note
        logger.warning("email ledger: could not count today's sends: %s", exc)
        return 0


async def record(kind: str, email_class: str, outcome: str, locale: str, to: str) -> None:
    try:
        await email_sends_collection.insert_one(ledger_row(kind, email_class, outcome, locale, to, datetime.now(timezone.utc)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("email ledger: could not record %s %s: %s", kind, outcome, exc)


async def note_resend_count(header_value: Optional[str]) -> None:
    """Resend says on each reply how many it has taken today; keep the highest we have seen."""
    try:
        count = int(header_value or "")
    except (TypeError, ValueError):
        return
    try:
        await email_sends_collection.update_one(
            {"_id": f"quota:{utc_day()}"}, {"$max": {"resend_count": count}, "$set": {"day": utc_day()}}, upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("email ledger: could not store Resend's count: %s", exc)
