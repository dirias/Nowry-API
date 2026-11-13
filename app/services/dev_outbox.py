"""
The dev-only outbox (ADR-090, FR-042).

When `DEV_OUTBOX` is on and the API is not production, every email the API
renders is also kept here, whatever Resend answered, so the dev end-to-end
suite can open verification, reset and leave links without a mailbox. Rows go
after three days. Production never keeps anything, and never mounts the route
that reads it (`app/routers/dev_outbox.py`).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.config import database
from app.utils.logger import get_logger

logger = get_logger(__name__)

COLLECTION = "dev_outbox"
KEEP_DAYS = 3
READ_LIMIT = 50
_PRODUCTION_NAMES = frozenset({"production", "prod"})
_index_ready = False


def _on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_production() -> bool:
    return (os.getenv("ENV") or "").strip().lower() in _PRODUCTION_NAMES


def enabled() -> bool:
    """Kept only when asked for, and never in production."""
    return _on("DEV_OUTBOX") and not is_production()


#: The suite's aliases (E2E-005): `rikydier+e2e-<run>-<case>@…`. Manual `rikydier+…` tests are not covered.
SUITE_PREFIX = "rikydier+e2e-"


def outbox_only(address: str) -> bool:
    """On development with the outbox on, a suite alias is kept here and never sent (ADR-090 amended)."""
    return enabled() and (address or "").strip().lower().startswith(SUITE_PREFIX)


def should_mount() -> bool:
    """Whether the app serves `GET /dev/outbox` at all."""
    return enabled()


async def _ensure_index() -> None:
    global _index_ready
    if _index_ready:
        return
    await database.db[COLLECTION].create_index("expire_at", expireAfterSeconds=0, name="dev_outbox_ttl")
    _index_ready = True


async def keep(kind: str, to: str, rendered: Any, outcome: str) -> None:
    """Store one rendered email. Never raises."""
    if not enabled():
        return
    now = datetime.now(timezone.utc)
    try:
        await _ensure_index()
        await database.db[COLLECTION].insert_one({
            "to": to.strip().lower(), "kind": kind, "outcome": outcome, "locale": rendered.locale,
            "subject": rendered.subject, "text": rendered.text, "html": rendered.html,
            "created_at": now, "expire_at": now + timedelta(days=KEEP_DAYS),
        })
    except Exception as exc:  # noqa: BLE001 - the outbox must never fail a send
        logger.warning("dev outbox: could not keep an email: %s", type(exc).__name__)


async def read(to: Optional[str], kind: Optional[str], limit: int) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    if to:
        query["to"] = to.strip().lower()
    if kind:
        query["kind"] = kind
    rows = await database.db[COLLECTION].find(query, {"_id": 0, "expire_at": 0}).sort("created_at", -1).to_list(length=min(limit, READ_LIMIT))
    for row in rows:
        row["created_at"] = row["created_at"].isoformat() if isinstance(row.get("created_at"), datetime) else row.get("created_at")
    return rows
