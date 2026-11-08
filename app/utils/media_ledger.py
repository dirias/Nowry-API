"""The media ledger: what the app has stored, and how it lets go of it (ADR-062).

One row per stored image — owner, book, storage id, bytes, format, size, when.
Deletion reads the ledger to find what to remove, the plan's monthly count is
taken from it, and a later backend migration reads it to know what exists.
Storage is never listed to learn what the app owns.

Removal is best effort: a storage failure is logged and never fails the user's
own request. The storage SDK runs in a thread executor, so a sweep stays off
the event loop even when it is awaited inside a request.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.config.database import media_collection
from app.utils.logger import get_logger
from app.utils.storage import get_storage_backend

logger = get_logger(__name__)

#: A sweep reads at most this many rows at once; a book or an account with
#: more is swept in rounds until the ledger is empty for it.
SWEEP_PAGE = 500


def _storage():
    return get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))


async def record(
    *,
    user_id: str,
    book_id: Optional[str],
    public_id: str,
    url: str,
    bytes_: Optional[int],
    format_: Optional[str],
    width: Optional[int],
    height: Optional[int],
) -> Dict[str, Any]:
    """Write one row for a stored image and return it.

    A re-upload to the same storage id (an avatar written with `overwrite`)
    replaces the row rather than adding one, so the ledger never names a
    stored object twice.
    """
    row = {
        "user_id": user_id,
        "book_id": book_id,
        "public_id": public_id,
        "url": url,
        "bytes": bytes_,
        "format": format_,
        "width": width,
        "height": height,
        "created_at": datetime.now(timezone.utc),
    }
    await media_collection.update_one({"public_id": public_id}, {"$set": row}, upsert=True)
    return row


async def rows_for_book(book_id: str, limit: int = SWEEP_PAGE) -> List[Dict[str, Any]]:
    """The ledger rows stored under one book."""
    return await media_collection.find({"book_id": book_id}).to_list(length=limit)


async def rows_for_user(user_id: str, limit: int = SWEEP_PAGE) -> List[Dict[str, Any]]:
    """Every ledger row an account owns, whatever book it was stored under."""
    return await media_collection.find({"user_id": user_id}).to_list(length=limit)


async def row_for_public_id(user_id: str, public_id: str) -> Optional[Dict[str, Any]]:
    """The owner's row for one storage id, if the ledger has it."""
    return await media_collection.find_one({"user_id": user_id, "public_id": public_id})


async def remove_rows(ids: List[ObjectId]) -> int:
    """Drop ledger rows by `_id`; returns how many went."""
    if not ids:
        return 0
    result = await media_collection.delete_many({"_id": {"$in": ids}})
    return int(result.deleted_count)


def month_start(now: Optional[datetime] = None) -> datetime:
    """Midnight UTC on the first of the current month."""
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def count_this_month(user_id: str) -> int:
    """How many images the account stored since the month began — the plan's unit."""
    return await media_collection.count_documents(
        {"user_id": user_id, "created_at": {"$gte": month_start()}}
    )


async def _sweep(rows_query: Dict[str, Any], label: str) -> int:
    """Delete every row matching the query from storage, then from the ledger.

    Returns how many rows the ledger lost. Storage is asked first: a row that
    outlives a failed storage call is swept again next time, whereas a storage
    object that outlives its row would be lost to the app for good.
    """
    removed = 0
    while True:
        rows = await media_collection.find(rows_query).to_list(length=SWEEP_PAGE)
        if not rows:
            return removed
        public_ids = [row["public_id"] for row in rows]
        try:
            gone = await _storage().delete_many(public_ids)
        except Exception:
            logger.error("Media sweep for %s could not reach storage; %d rows kept", label, len(rows), exc_info=True)
            return removed
        if gone < len(public_ids):
            logger.warning("Media sweep for %s: storage confirmed %d of %d deletions", label, gone, len(public_ids))
        removed += await remove_rows([row["_id"] for row in rows])


async def sweep_book(book_id: str) -> int:
    """Remove a deleted book's images from storage and the ledger. Best effort, logged."""
    try:
        removed = await _sweep({"book_id": book_id}, f"book {book_id}")
    except Exception:
        logger.error("Media sweep for book %s failed", book_id, exc_info=True)
        return 0
    if removed:
        logger.info("Media sweep for book %s removed %d images", book_id, removed)
    return removed


async def sweep_user(user_id: str) -> int:
    """Remove every image an account stored, orphans included. Best effort, logged."""
    try:
        removed = await _sweep({"user_id": user_id}, f"user {user_id}")
    except Exception:
        logger.error("Media sweep for user %s failed", user_id, exc_info=True)
        return 0
    if removed:
        logger.info("Media sweep for user %s removed %d images", user_id, removed)
    return removed
