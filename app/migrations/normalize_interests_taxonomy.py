"""
One-time, idempotent normalisation of ``preferences.general.interests`` and
``preferences.general.primary_topic`` onto the canonical topic taxonomy.

Background
----------
The onboarding wizard wrote lowercase snake_case values (``'technology'``) while
the account-settings screen wrote Title Case (``'Technology'``), so existing user
documents hold mixed-case duplicates such as ``['technology', 'Technology']``.
``app/routers/agent.py`` feeds this array straight into the AI persona prompt, so
the corruption reaches model output. Separately, ``primary_topic`` was collected
as its own wizard question and can hold a value absent from ``interests``.

Per document this migration:
  1. lowercases every interest value,
  2. maps legacy aliases onto their taxonomy value,
  3. drops values outside the 14-value taxonomy,
  4. de-duplicates preserving order (``interests[0]`` becomes ``primary_topic``),
  5. caps the list at ``MAX_INTERESTS``,
  6. re-points ``primary_topic`` at ``interests[0]`` when the stored value is not
     in the normalised list (``None`` when the list is empty).

Standalone script — NOT wired into app startup. Run from ``Nowry-API``:

    .venv/bin/python -m app.migrations.normalize_interests_taxonomy           # dry run
    .venv/bin/python -m app.migrations.normalize_interests_taxonomy --apply   # write

Dry run is the default and touches nothing. Safe to re-run: normalisation is
idempotent, and a document is only written when its normalised form differs from
what is already stored, so a second pass reports zero updates.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, UpdateOne

from app.config.database import users_collection
from app.models.topics import derive_primary_topic, normalize_interests
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Documents fetched per page. Bounded — never ``to_list(None)``.
BATCH_SIZE: int = 500

#: Only documents that actually carry an interests array need inspecting.
MIGRATION_FILTER: dict[str, Any] = {
    "preferences.general.interests": {"$exists": True},
}

#: Fetch only the fields the migration reads, to keep each page small.
PROJECTION: dict[str, int] = {
    "_id": 1,
    "preferences.general.interests": 1,
    "preferences.general.primary_topic": 1,
}


class MigrationStats:
    """Mutable counters for a single migration run."""

    def __init__(self) -> None:
        self.scanned: int = 0
        self.interests_changed: int = 0
        self.primary_topic_changed: int = 0
        self.documents_touched: int = 0
        self.documents_written: int = 0


def _build_update(document: dict[str, Any], stats: MigrationStats) -> UpdateOne | None:
    """
    Return the ``UpdateOne`` needed to normalise one document, or ``None`` when
    the document is already canonical.

    Increments the counters on ``stats`` as a side effect.
    """
    general: dict[str, Any] = (document.get("preferences") or {}).get("general") or {}

    stored_interests: Any = general.get("interests")
    stored_primary_topic: Any = general.get("primary_topic")

    normalized_interests: list[str] = normalize_interests(stored_interests)
    normalized_primary_topic: str | None = derive_primary_topic(
        stored_primary_topic, normalized_interests
    )

    interests_differs: bool = normalized_interests != stored_interests
    primary_topic_differs: bool = normalized_primary_topic != stored_primary_topic

    if not interests_differs and not primary_topic_differs:
        return None

    if interests_differs:
        stats.interests_changed += 1
    if primary_topic_differs:
        stats.primary_topic_changed += 1
    stats.documents_touched += 1

    return UpdateOne(
        {"_id": document["_id"]},
        {
            "$set": {
                "preferences.general.interests": normalized_interests,
                "preferences.general.primary_topic": normalized_primary_topic,
            }
        },
    )


async def _fetch_page(after_id: ObjectId | None) -> list[dict[str, Any]]:
    """Fetch one bounded page of candidate documents, ordered by ``_id``."""
    query: dict[str, Any] = dict(MIGRATION_FILTER)
    if after_id is not None:
        query["_id"] = {"$gt": after_id}

    return await (
        users_collection.find(query, PROJECTION)
        .sort("_id", ASCENDING)
        .to_list(length=BATCH_SIZE)
    )


async def normalize_interests_taxonomy(apply_changes: bool = False) -> MigrationStats:
    """
    Page through every user document holding an interests array and normalise it.

    When ``apply_changes`` is False (the default) the run is a dry run: it reports
    exactly what would change and issues no writes.
    """
    mode: str = "APPLY" if apply_changes else "DRY RUN"
    logger.info(f"Starting interests taxonomy normalisation [{mode}], batch size {BATCH_SIZE}.")

    stats = MigrationStats()
    after_id: ObjectId | None = None
    batch_number: int = 0

    while True:
        page: list[dict[str, Any]] = await _fetch_page(after_id)
        if not page:
            break

        batch_number += 1
        stats.scanned += len(page)

        operations: list[UpdateOne] = []
        for document in page:
            operation = _build_update(document, stats)
            if operation is not None:
                operations.append(operation)

        if operations and apply_changes:
            result = await users_collection.bulk_write(operations, ordered=False)
            stats.documents_written += result.modified_count

        logger.info(
            f"Batch {batch_number}: scanned {len(page)}, "
            f"needing normalisation {len(operations)}."
        )

        if len(page) < BATCH_SIZE:
            break
        after_id = page[-1]["_id"]

    logger.info(
        f"Normalisation complete [{mode}]. "
        f"Scanned {stats.scanned} document(s); "
        f"{stats.documents_touched} need changes "
        f"({stats.interests_changed} interests, {stats.primary_topic_changed} primary_topic); "
        f"{stats.documents_written} written."
    )
    if not apply_changes and stats.documents_touched:
        logger.info("Dry run — no documents were modified. Re-run with --apply to write.")

    return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalise preferences.general.interests and primary_topic onto the "
            "canonical topic taxonomy. Dry run unless --apply is passed."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the normalised values. Without this flag the run is read-only.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(normalize_interests_taxonomy(apply_changes=args.apply))
