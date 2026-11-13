"""
A fixed-window counter for any key, stored in Mongo (ADR-078 point 8).

`enforce_user_rate_limit` beside this serves a signed-in account and raises.
The email routes need to limit things that are not accounts (a caller's
address, a hash of an email that may not exist) and to answer in their own
words, so this one returns the seconds to wait and never raises. The counters
share `rate_limits`, so they hold across workers and restarts.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config.database import rate_limits_collection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def bucket(feature: str, key: str, window_seconds: int, now: float) -> tuple[str, int, datetime]:
    """The row's id, the window's start, and when the row may be removed."""
    start = int(now // window_seconds) * window_seconds
    expires = datetime.fromtimestamp(start, tz=timezone.utc) + timedelta(seconds=window_seconds * 2)
    return f"{feature}:{key}:{start}", start, expires


async def seconds_to_wait(feature: str, key: str, limit: int, window_seconds: int) -> int:
    """Take one slot. 0 when the caller may go on, else the seconds until the window ends.

    A counter that cannot be read refuses for a minute: a limit that lets
    everything through when its store is down is not a limit.
    """
    now = time.time()
    bucket_id, start, expires = bucket(feature, key, window_seconds, now)
    try:
        row = await rate_limits_collection.find_one_and_update(
            {"_id": bucket_id},
            {"$inc": {"count": 1}, "$setOnInsert": {"feature": feature, "window_start": start, "expires_at": expires}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("keyed_rate_limit: counter unavailable for %s: %s", feature, exc)
        return 60
    if int((row or {}).get("count", 1)) > limit:
        return max(1, int(start + window_seconds - now))
    return 0


async def longest_wait(checks: list[tuple[str, str, int, int]]) -> int:
    """Take a slot in every (feature, key, limit, window) and return the longest wait among them."""
    waits = [await seconds_to_wait(*check) for check in checks]
    return max(waits, default=0)


async def claim_rolling(feature: str, key: str, window_seconds: int, now: datetime | None = None) -> bool:
    """Once per key in any `window_seconds` (a rolling window, not a fixed one). True when the caller may go on.

    One atomic write: the row is taken when it is missing or older than the
    window; a row inside the window makes the upsert collide, which is a no.
    A store that cannot be read says no.
    """
    at = now or datetime.now(timezone.utc)
    row_id = f"{feature}:{key}"
    try:
        await rate_limits_collection.find_one_and_update(
            {"_id": row_id, "$or": [{"last_at": {"$exists": False}}, {"last_at": {"$lt": at - timedelta(seconds=window_seconds)}}]},
            {"$set": {"feature": feature, "last_at": at, "expires_at": at + timedelta(seconds=window_seconds)}},
            upsert=True,
        )
        return True
    except DuplicateKeyError:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("keyed_rate_limit: rolling claim unavailable for %s: %s", feature, exc)
        return False
