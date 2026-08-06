"""
Per-user rate limiting backed by MongoDB.

Why not SlowAPI (``app.core.limiter``)?
  * Its shared limiter keys on the client IP (``get_remote_address``), but quota
    for a billed AI feature must follow the *account*, not the network path — a
    single user on a rotating mobile IP would otherwise get a fresh allowance
    per request, and an office NAT would share one allowance across colleagues.
  * ``main.py`` registers SlowAPI's global ``_rate_limit_exceeded_handler``, so
    every SlowAPI 429 renders one fixed body. Overriding it to customise a
    single route's message would change the 429 body for every other rate
    limited route (quiz, agent, …).

The counter is a fixed window stored in Mongo rather than an in-process dict so
the quota holds across Uvicorn workers and survives restarts — an in-memory
counter would hand each worker its own full allowance.

This complements, and does not replace, the global SlowAPI IP limit.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pymongo import ReturnDocument

from app.config.database import rate_limits_collection
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def enforce_user_rate_limit(
    user_id: str,
    feature: str,
    limit: int,
    window_seconds: int,
    detail: str,
) -> int:
    """Consume one slot of a per-user fixed-window quota, or raise 429.

    The read-modify-write is a single atomic ``find_one_and_update`` keyed on a
    deterministic ``_id``, so concurrent requests from the same user cannot race
    past the limit (the unique ``_id`` serialises the upsert).

    A rejected request still increments the counter. That is deliberate: the
    window is fixed, so it resets on schedule regardless of the count, and the
    extra increments can never extend a lockout — they only stop a caller who is
    already over quota from spending Mongo round-trips to discover it.

    Args:
        user_id: MongoDB user id of the caller (``current_user["user_id"]``).
        feature: Namespace for the quota, e.g. ``"tts_amagic"``.
        limit: Maximum requests allowed per window.
        window_seconds: Window length in seconds.
        detail: User-facing ``detail`` string for the 429 response.

    Returns:
        The caller's request count within the current window (always <= limit).

    Raises:
        HTTPException: 429 when the quota is exhausted; 500 when the counter
            itself cannot be read or written.
    """
    if not user_id:
        # No identity means no per-user quota can be enforced. Refusing is the
        # only safe option — the alternative is an unmetered billed call.
        raise HTTPException(status_code=401, detail="Authentication required.")

    now: float = time.time()
    window_start: int = int(now // window_seconds) * window_seconds
    bucket_id: str = f"{feature}:{user_id}:{window_start}"

    # Keep the document one full window past its own expiry so the TTL monitor
    # (which runs about once a minute) can never delete a bucket still in use.
    expires_at: datetime = datetime.fromtimestamp(
        window_start, tz=timezone.utc
    ) + timedelta(seconds=window_seconds * 2)

    try:
        bucket = await rate_limits_collection.find_one_and_update(
            {"_id": bucket_id},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "user_id": user_id,
                    "feature": feature,
                    "window_start": window_start,
                    "expires_at": expires_at,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except Exception as exc:
        # Fail closed. This guard exists to bound spend on a per-character
        # billed provider; letting requests through unmetered because the meter
        # broke inverts the entire point of the limit.
        logger.exception(
            f"[rate_limit] counter unavailable for feature={feature} "
            f"user_id={user_id}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to verify your usage quota. Please try again.",
        )

    count: int = int(bucket.get("count", 1)) if bucket else 1

    if count > limit:
        retry_after: int = max(1, int(window_start + window_seconds - now))
        logger.warning(
            f"[rate_limit] quota exceeded for feature={feature} "
            f"user_id={user_id} count={count} limit={limit}"
        )
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(retry_after)},
        )

    return count
