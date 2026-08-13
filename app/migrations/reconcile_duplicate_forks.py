"""
Bounded, repeatable reconciliation of duplicate ``content_forks`` records.

Background
----------
Before ADR-005 the fork service checked ``content_forks`` and then inserted,
with no database constraint between the two steps. Two requests in flight at
once could both pass the check, so a key can hold more than one record —
sometimes pointing at more than one private copy of the same source.

``FORK_UNIQUE_INDEX`` in ``app/config/database.py`` makes
``(original_content_type, original_content_id, forked_by_user_id)`` unique.
MongoDB refuses to build a unique index over existing duplicates, so **this
migration must run to completion before that index can be created**. Startup
logs the failure and names this module when duplicates are still present.

Per duplicated key this migration:
  1. loads every record for the key (bounded page),
  2. classifies each one by whether its forked content is still live,
  3. keeps one canonical record — the earliest fork whose content is still
     live, or the earliest record overall when none is,
  4. deletes the surplus records so the key holds exactly one.

**It never deletes content.** A surplus record's private deck or book stays in
the user's library as an ordinary untracked document; deleting a user's study
material to satisfy an index is not a trade this migration is allowed to make.
Every such document is reported so it can be reviewed by hand.

Standalone script — NOT wired into app startup. Run from ``Nowry-API``:

    .venv/bin/python -m app.migrations.reconcile_duplicate_forks           # dry run
    .venv/bin/python -m app.migrations.reconcile_duplicate_forks --apply   # write

Dry run is the default and touches nothing. Safe to re-run: a key that already
holds one record is not a duplicate and is never visited, so a second pass over
a reconciled collection reports zero groups.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING

from app.config.database import content_forks_collection, db
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Duplicate key groups fetched per aggregation page. Bounded — never
#: ``to_list(None)``.
BATCH_SIZE: int = 500

#: Records inspected per key in one pass. A key with more than this many
#: records is partially reconciled and converges over repeated runs, which is
#: why the migration is designed to be re-run rather than to hold everything in
#: memory.
MAX_RECORDS_PER_KEY: int = 200

#: Hard ceiling on duplicate keys collected in one run, so a pathological
#: collection cannot exhaust memory. Exceeding it is reported and the run stops
#: cleanly; the next run continues from a smaller collection.
MAX_DUPLICATE_KEYS: int = 20000

KEY_FIELDS = ("original_content_type", "original_content_id", "forked_by_user_id")


class ReconciliationStats:
    """Mutable counters for a single reconciliation run."""

    def __init__(self) -> None:
        self.duplicate_keys: int = 0
        self.records_scanned: int = 0
        self.records_deleted: int = 0
        self.keys_with_live_content: int = 0
        self.keys_without_live_content: int = 0
        self.orphaned_content: list = []
        self.keys_truncated: int = 0


def _duplicate_group_pipeline(skip: int) -> list:
    """Aggregation returning one page of keys that hold more than one record."""
    return [
        {"$group": {
            "_id": {field: f"${field}" for field in KEY_FIELDS},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"_id": ASCENDING}},
        {"$skip": skip},
        {"$limit": BATCH_SIZE},
    ]


async def _scan_duplicate_keys() -> tuple:
    """Collect every duplicated key. Read-only.

    Returns ``(keys, truncated)``. Paging is by ``$skip`` over a sorted grouped
    result, which is stable because this phase issues no writes.
    """
    keys: list = []
    skip: int = 0

    while True:
        page = await content_forks_collection.aggregate(
            _duplicate_group_pipeline(skip), allowDiskUse=True
        ).to_list(length=BATCH_SIZE)
        if not page:
            break

        for group in page:
            keys.append({field: group["_id"].get(field) for field in KEY_FIELDS})
            if len(keys) >= MAX_DUPLICATE_KEYS:
                logger.warning(
                    f"Stopping the scan at {MAX_DUPLICATE_KEYS} duplicate keys. "
                    "Re-run after applying to reconcile the remainder."
                )
                return keys, True

        if len(page) < BATCH_SIZE:
            break
        skip += BATCH_SIZE

    return keys, False


async def _content_is_live(content_type: str, content_id: Any, owner: Any) -> bool:
    """True when a record's forked content still exists for its owner."""
    if not content_id:
        return False
    try:
        content_oid = ObjectId(str(content_id))
    except Exception:
        return False

    document = await db[f"{content_type}s"].find_one(
        {"_id": content_oid, "user_id": owner, "deleted_at": None}, {"_id": 1}
    )
    return document is not None


async def _load_key_records(key: dict, stats: ReconciliationStats) -> list:
    """Load one bounded page of the records stored under a key, oldest first."""
    records = await (
        content_forks_collection.find(key)
        .sort([("forked_at", ASCENDING), ("_id", ASCENDING)])
        .to_list(length=MAX_RECORDS_PER_KEY)
    )
    if len(records) == MAX_RECORDS_PER_KEY:
        stats.keys_truncated += 1
    return records


async def _choose_canonical(key: dict, records: list) -> tuple:
    """Pick the record to keep and the ones to delete.

    The canonical record is the earliest fork whose content is still live: it is
    the copy the user has had longest, and deterministic ordering means repeated
    runs agree. When no content survives, the earliest record is kept and the
    service's existing stale-record rule lets the user fork again.
    """
    content_type = key["original_content_type"]
    owner = key["forked_by_user_id"]

    live: list = []
    for record in records:
        if await _content_is_live(content_type, record.get("forked_content_id"), owner):
            live.append(record)

    canonical = live[0] if live else records[0]
    surplus = [record for record in records if record["_id"] != canonical["_id"]]
    return canonical, surplus, bool(live)


async def _reconcile_key(key: dict, apply_changes: bool, stats: ReconciliationStats) -> None:
    """Reduce one duplicated key to a single record."""
    records = await _load_key_records(key, stats)
    if len(records) < 2:
        return

    stats.duplicate_keys += 1
    stats.records_scanned += len(records)

    canonical, surplus, had_live = await _choose_canonical(key, records)
    if had_live:
        stats.keys_with_live_content += 1
    else:
        stats.keys_without_live_content += 1

    for record in surplus:
        content_id = record.get("forked_content_id")
        if content_id and content_id != canonical.get("forked_content_id"):
            stats.orphaned_content.append(
                f"{key['original_content_type']}:{content_id} "
                f"(user {key['forked_by_user_id']})"
            )
        if apply_changes:
            result = await content_forks_collection.delete_one({"_id": record["_id"]})
            stats.records_deleted += result.deleted_count
        else:
            stats.records_deleted += 1


async def reconcile_duplicate_forks(apply_changes: bool = False) -> ReconciliationStats:
    """
    Reduce every duplicated fork key to one record so the unique index can build.

    When ``apply_changes`` is False (the default) the run is a dry run: it
    reports exactly which records would be removed and issues no writes.
    """
    mode: str = "APPLY" if apply_changes else "DRY RUN"
    logger.info(f"Starting fork reconciliation [{mode}], batch size {BATCH_SIZE}.")

    stats = ReconciliationStats()
    keys, truncated = await _scan_duplicate_keys()
    logger.info(f"Found {len(keys)} duplicated fork key(s).")

    for key in keys:
        await _reconcile_key(key, apply_changes, stats)

    logger.info(
        f"Fork reconciliation complete [{mode}]. "
        f"{stats.duplicate_keys} duplicated key(s); "
        f"{stats.records_scanned} record(s) scanned; "
        f"{stats.records_deleted} surplus record(s) "
        f"{'deleted' if apply_changes else 'would be deleted'}; "
        f"{stats.keys_with_live_content} key(s) kept a live fork, "
        f"{stats.keys_without_live_content} key(s) had none."
    )
    if stats.orphaned_content:
        logger.warning(
            f"{len(stats.orphaned_content)} forked document(s) are no longer tracked "
            f"by a fork record and were NOT deleted: {stats.orphaned_content[:50]}"
        )
    if stats.keys_truncated:
        logger.warning(
            f"{stats.keys_truncated} key(s) hold more than {MAX_RECORDS_PER_KEY} "
            "records and were only partially reconciled. Re-run to continue."
        )
    if truncated:
        logger.warning("Key scan was truncated — re-run after applying.")
    if not apply_changes and stats.records_deleted:
        logger.info("Dry run — nothing was modified. Re-run with --apply to write.")

    return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reduce duplicate content_forks records to one per "
            "(content type, source, user) so the unique fork index can be built. "
            "Dry run unless --apply is passed. Never deletes user content."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the surplus fork records. Without this flag the run is read-only.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(reconcile_duplicate_forks(apply_changes=args.apply))
