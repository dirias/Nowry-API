"""The media ledger: what the app has stored, and how it lets go of it (ADR-062).

One row per stored image — owner, book, storage id, bytes, format, size, when.
Deletion reads the ledger to find what to remove, the plan's monthly count is
taken from it, and a later backend migration reads it to know what exists.
Storage is never listed to learn what the app owns.

Removal never fails the user's own request, and it is durable (ADR-065): the
request marks the rows, and a reaper removes them from storage and then from
the ledger, retrying with a growing delay until storage has answered. The
storage SDK runs in a thread executor, so removal stays off the event loop.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

from app.config.database import media_collection
from app.utils.logger import get_logger
from app.utils.storage import get_storage_backend

logger = get_logger(__name__)

#: The `origin` of a ledger row written for a picture that came with an imported file.
ORIGIN_IMPORT = "import"

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
    origin: Optional[str] = None,
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
    if origin:
        # How the image came to be stored. "import" is what the monthly count
        # leaves out (ADR-067): a file's pictures are not the reader's uploads.
        row["origin"] = origin
    await media_collection.update_one(
        {"public_id": public_id},
        # A storage id written again is wanted again: whatever marked it for removal no longer holds.
        {"$set": row, "$unset": {"delete_after": "", "delete_attempts": "", "delete_lease_until": "", "delete_lease_owner": ""}},
        upsert=True,
    )
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
    """How many images the account uploaded since the month began — the plan's unit.

    Pictures that arrived inside an imported file are not counted (ADR-067):
    importing is free, and an import has its own ceiling per document.
    """
    return await media_collection.count_documents(
        {"user_id": user_id, "created_at": {"$gte": month_start()}, "origin": {"$ne": ORIGIN_IMPORT}}
    )


# ── letting go, durably (ADR-065) ───────────────────────────────────────────
#
# Removal used to be one attempt in a background task after the response: a
# storage error, a rate limit ("too many concurrent destroy operations" when
# several documents were deleted together) or a restart before the task ran
# left the images stored for good, with nothing to try again.
#
# Now the decision and the work are separate. `condemn` writes the decision on
# the rows themselves, inside the user's request, so it survives anything.
# `collect` does the work: it leases a batch of condemned rows, asks storage to
# delete them, and drops the rows only when storage has answered; a failure
# puts the batch back with a later `delete_after`. `reaper_loop` runs `collect`
# for the life of the process, so what one attempt could not remove the next
# one does, and so do rows a crashed process left behind.

#: Rows one `collect` round leases; also Cloudinary's Admin API batch size.
COLLECT_BATCH = 100
#: Rounds one call may run before it yields, so a huge backlog cannot hold the loop.
COLLECT_MAX_ROUNDS = 20
#: How long a leased batch is another worker's to leave alone.
LEASE_SECONDS = 300
#: First retry delay; doubles per attempt up to the cap.
RETRY_BASE_SECONDS = 60
RETRY_CAP_SECONDS = 6 * 60 * 60
#: Seconds between two runs of the reaper; 0 turns the loop off.
REAPER_INTERVAL_SECONDS = int(os.getenv("MEDIA_REAPER_INTERVAL_SECONDS", "300"))
#: Uncondemned rows examined per run when looking for images of deleted books.
ORPHAN_SCAN_PAGE = 500

# One storage deletion at a time per process: the provider refuses concurrent
# destroy calls, and nothing is waiting on this work.
_storage_gate = asyncio.Lock()
_orphan_cursor: Optional[ObjectId] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retry_delay(attempts: int) -> int:
    """Seconds to wait before attempt number `attempts + 1`."""
    return min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, attempts)))


async def condemn(rows_query: Dict[str, Any]) -> int:
    """Mark every matching row for removal. Durable, idempotent, and cheap enough to await in a request."""
    result = await media_collection.update_many(
        {**rows_query, "delete_after": {"$exists": False}},
        {"$set": {"delete_after": _now(), "delete_attempts": 0}},
    )
    return int(result.modified_count)


async def condemn_for(rows_query: Dict[str, Any], label: str) -> int:
    """`condemn` for a request: a ledger failure is logged, never raised into the user's deletion.

    A book deletion that could not mark its rows is still found later by
    `condemn_orphans`, which reads the book's own `deleted_at`.
    """
    try:
        return await condemn(rows_query)
    except Exception:
        logger.error("Could not mark the media of %s for removal", label, exc_info=True)
        return 0


async def _lease(rows_query: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Claim up to one batch of due, unleased, condemned rows for this worker."""
    now = _now()
    due = {
        **rows_query,
        "delete_after": {"$lte": now},
        "$or": [{"delete_lease_until": {"$exists": False}}, {"delete_lease_until": {"$lte": now}}],
    }
    candidates = await media_collection.find(due, {"_id": 1}).limit(COLLECT_BATCH).to_list(length=COLLECT_BATCH)
    if not candidates:
        return []
    token = str(ObjectId())
    await media_collection.update_many(
        {**due, "_id": {"$in": [row["_id"] for row in candidates]}},
        {"$set": {"delete_lease_until": now + timedelta(seconds=LEASE_SECONDS), "delete_lease_owner": token}},
    )
    return await media_collection.find({"delete_lease_owner": token}).to_list(length=COLLECT_BATCH)


async def _release_for_retry(rows: List[Dict[str, Any]]) -> None:
    """Put a batch storage would not take back in the queue, each row later than its last try."""
    now = _now()
    for attempts in sorted({int(row.get("delete_attempts") or 0) for row in rows}):
        ids = [row["_id"] for row in rows if int(row.get("delete_attempts") or 0) == attempts]
        await media_collection.update_many(
            {"_id": {"$in": ids}},
            {"$set": {"delete_after": now + timedelta(seconds=retry_delay(attempts)), "delete_attempts": attempts + 1},
             "$unset": {"delete_lease_until": "", "delete_lease_owner": ""}},
        )


async def collect(rows_query: Optional[Dict[str, Any]] = None, label: str = "condemned media") -> int:
    """Remove condemned rows that are due: storage first, then the ledger. Returns rows removed.

    Storage is asked first: a row that outlives a failed storage call is tried
    again, whereas a stored object that outlived its row would be lost to the
    app for good. A failure is logged and the batch is rescheduled; it is never
    raised into a request.
    """
    removed = 0
    for _ in range(COLLECT_MAX_ROUNDS):
        rows = await _lease(rows_query or {})
        if not rows:
            break
        try:
            async with _storage_gate:
                gone = await _storage().delete_many([row["public_id"] for row in rows])
        except Exception:
            await _release_for_retry(rows)
            logger.error("Media removal for %s could not reach storage; %d rows will be retried",
                         label, len(rows), exc_info=True)
            break
        if gone < len(rows):
            logger.warning("Media removal for %s: storage confirmed %d of %d deletions", label, gone, len(rows))
        removed += await remove_rows([row["_id"] for row in rows])
    return removed


#: How long a row may name a book that does not exist before it is an orphan. An
#: import stores its pictures before its document (ADR-062 point 2), so for the
#: length of one import that is the ordinary state of things.
ORPHAN_GRACE_SECONDS = int(os.getenv("MEDIA_ORPHAN_GRACE_SECONDS", "3600"))


async def condemn_orphans(books_collection: Any, limit: int = ORPHAN_SCAN_PAGE) -> int:
    """Condemn the images of books that are deleted, or that were never saved.

    A deleted book's rows are here when its deletion ended before they were
    marked. A missing book's rows are an import that stored its pictures and
    then died before its document was written (ADR-071 point 1); they are
    orphans once older than `ORPHAN_GRACE_SECONDS`. The ledger is walked a page
    per call, by `_id`, and starts over at its end.
    """
    global _orphan_cursor
    query: Dict[str, Any] = {"delete_after": {"$exists": False}, "book_id": {"$ne": None}}
    if _orphan_cursor is not None:
        query["_id"] = {"$gt": _orphan_cursor}
    rows = await media_collection.find(query, {"book_id": 1}).sort("_id", 1).limit(limit).to_list(length=limit)
    _orphan_cursor = rows[-1]["_id"] if len(rows) == limit else None
    book_ids = sorted({str(row["book_id"]) for row in rows if ObjectId.is_valid(str(row["book_id"]))})
    if not book_ids:
        return 0
    books = await books_collection.find(
        {"_id": {"$in": [ObjectId(b) for b in book_ids]}}, {"deleted_at": 1}
    ).to_list(length=len(book_ids))
    deleted = [str(book["_id"]) for book in books if book.get("deleted_at") is not None]
    missing = sorted(set(book_ids) - {str(book["_id"]) for book in books})
    condemned = 0
    if deleted:
        condemned += await condemn({"book_id": {"$in": deleted}})
    if missing:
        cutoff = _now() - timedelta(seconds=ORPHAN_GRACE_SECONDS)
        condemned += await condemn({"book_id": {"$in": missing}, "created_at": {"$lt": cutoff}})
    return condemned


async def reap_once(books_collection: Any = None) -> int:
    """One pass of the reaper: find orphans, then remove everything that is due."""
    try:
        if books_collection is not None:
            await condemn_orphans(books_collection)
        removed = await collect()
    except Exception:
        logger.error("Media reaper pass failed", exc_info=True)
        return 0
    if removed:
        logger.info("Media reaper removed %d images", removed)
    return removed


async def reaper_loop(books_collection: Any = None, interval: Optional[int] = None) -> None:
    """Run the reaper for the life of the process. Cancelled at shutdown."""
    interval = REAPER_INTERVAL_SECONDS if interval is None else interval
    if interval <= 0:
        return
    while True:
        await reap_once(books_collection)
        await asyncio.sleep(interval)


async def sweep_book(book_id: str) -> int:
    """Remove a deleted book's images now, if storage allows. Best effort, logged.

    The rows are already condemned by the request (`condemn`); this is the first
    attempt, and the reaper makes the later ones.
    """
    try:
        await condemn({"book_id": book_id})
        removed = await collect({"book_id": book_id}, f"book {book_id}")
    except Exception:
        logger.error("Media sweep for book %s failed", book_id, exc_info=True)
        return 0
    if removed:
        logger.info("Media sweep for book %s removed %d images", book_id, removed)
    return removed


async def sweep_user(user_id: str) -> int:
    """Remove every image an account stored, orphans included. Best effort, logged."""
    try:
        await condemn({"user_id": user_id})
        removed = await collect({"user_id": user_id}, f"user {user_id}")
    except Exception:
        logger.error("Media sweep for user %s failed", user_id, exc_info=True)
        return 0
    if removed:
        logger.info("Media sweep for user %s removed %d images", user_id, removed)
    return removed
