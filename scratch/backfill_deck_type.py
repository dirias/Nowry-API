"""
One-time, idempotent backfill: set deck_type = "flashcard" on every deck
document that is missing the field or has it set to null.

Standalone script — NOT wired into app startup. Run manually:

    cd Nowry-API
    .venv/bin/python scratch/backfill_deck_type.py

Safe to re-run: the filter only ever matches documents that still need the
backfill, so subsequent runs are no-ops.
"""

import asyncio
import sys

sys.path.append("/Users/CVYV0H267P-didier/Nowry/Nowry-API")

from app.config.database import decks_collection
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def backfill_deck_type() -> None:
    missing_or_null_filter = {
        "$or": [
            {"deck_type": {"$exists": False}},
            {"deck_type": None},
        ]
    }

    to_update = await decks_collection.count_documents(missing_or_null_filter)
    logger.info(f"Found {to_update} deck(s) missing deck_type.")

    if to_update == 0:
        logger.info("Nothing to backfill.")
        return

    result = await decks_collection.update_many(
        missing_or_null_filter,
        {"$set": {"deck_type": "flashcard"}},
    )
    logger.info(
        f"Backfill complete: matched {result.matched_count}, "
        f"modified {result.modified_count}."
    )


if __name__ == "__main__":
    asyncio.run(backfill_deck_type())
