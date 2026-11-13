"""
Switch the study buddy on for accounts that never had it (ROAM-002, docs/prd-road-to-market.md FR-037).

Background
----------
Until ROAM-002 a new account was stored with `pet_active: false` and
`pet_revealed: false`, and only the web switched the companion on, after a
first finished study session. The phone never did, so an account could go on
seeing no companion at all. From FR-037 the study buddy is on by default and
rests in one place (roaming stays off, FR-036).

What it does: every account whose companion was never revealed
(`preferences.pet.pet_revealed: false`) gets `pet_active: true` and
`pet_revealed: true`. Never revealed means the off value is the old sign-up
default, not a choice. An account whose companion was revealed and then turned
off is left alone, as is an account with no stored value (it already reads as
on). Nothing else is touched and nothing is deleted.

Repeatable: a second run finds nothing to change.

Standalone script, NOT wired into app startup. Run from ``Nowry-API``:

    .venv/bin/python -m app.migrations.companion_on_by_default            # dry run
    .venv/bin/python -m app.migrations.companion_on_by_default --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("companion_on_by_default")

NEVER_REVEALED: Dict[str, Any] = {"preferences.pet.pet_revealed": False}


async def companion_on_by_default(apply_changes: bool = False, collection: Optional[Any] = None) -> Dict[str, int]:
    """Count accounts whose companion was never revealed; with `apply_changes`, switch it on.

    Returns ``{"found": n, "changed": m}``. A dry run reports ``changed: 0``.
    """
    if collection is None:
        from app.config.database import users_collection as collection  # noqa: PLC0415 - no DB at import

    found: int = await collection.count_documents(NEVER_REVEALED)
    changed: int = 0
    if apply_changes and found:
        result = await collection.update_many(
            NEVER_REVEALED,
            {"$set": {
                "preferences.pet.pet_active": True,
                "preferences.pet.pet_revealed": True,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        changed = result.modified_count
    logger.info(
        "[%s] accounts whose companion was never revealed: %d; switched on: %d",
        "APPLY" if apply_changes else "DRY RUN",
        found,
        changed,
    )
    if not apply_changes and found:
        logger.info("Dry run: nothing was modified. Re-run with --apply to write.")
    return {"found": found, "changed": changed}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Switch the study buddy on for accounts that never had it. Dry run unless --apply is passed.")
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    asyncio.run(companion_on_by_default(apply_changes=args.apply))
