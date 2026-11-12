"""
Turn a stored "email digest: on" off (MAIL-002, docs/prd-road-to-market.md FR-017).

Background
----------
`email_digest` used to default to on, and the settings call could store that
value. No digest was ever sent and no screen shows the switch today, so a
stored `true` is a default nobody chose. Before any digest exists, every
account starts from off; a person who wants one turns it on.

What it does: sets `notification_preferences.email_digest` to `false` on every
user document where it is stored as `true`. Accounts with no stored value are
left alone; the API's default already reads as off for them. The other
preferences are not touched. Nothing is deleted.

Repeatable: a second run finds nothing to change.

Standalone script, NOT wired into app startup. Run from ``Nowry-API``:

    .venv/bin/python -m app.migrations.email_digest_default_off            # dry run
    .venv/bin/python -m app.migrations.email_digest_default_off --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("email_digest_default_off")

STORED_ON: Dict[str, Any] = {"notification_preferences.email_digest": True}


async def email_digest_default_off(apply_changes: bool = False, collection: Optional[Any] = None) -> Dict[str, int]:
    """Count the accounts storing `email_digest: true`; with `apply_changes`, turn them off.

    Returns ``{"found": n, "changed": m}``. A dry run reports ``changed: 0``.
    """
    if collection is None:
        from app.config.database import users_collection as collection  # noqa: PLC0415 - no DB at import

    found: int = await collection.count_documents(STORED_ON)
    changed: int = 0
    if apply_changes and found:
        result = await collection.update_many(
            STORED_ON,
            {"$set": {"notification_preferences.email_digest": False, "updated_at": datetime.now(timezone.utc)}},
        )
        changed = result.modified_count
    logger.info(
        "[%s] accounts storing email_digest=true: %d; turned off: %d",
        "APPLY" if apply_changes else "DRY RUN",
        found,
        changed,
    )
    if not apply_changes and found:
        logger.info("Dry run: nothing was modified. Re-run with --apply to write.")
    return {"found": found, "changed": changed}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Turn a stored email_digest=true off. Dry run unless --apply is passed.")
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    asyncio.run(email_digest_default_off(apply_changes=args.apply))
