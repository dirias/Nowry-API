"""
The admin log (ADMIN-005, ADR-086 point 4, docs/prd-admin.md FR-003).

Every admin write calls `record_admin_action` once: who acted, which fixed
action, on what, why, and when. Rows live in `admin_actions` and expire after
24 months (the TTL index in `app/config/database.py`). Writing the log never
fails the operation it records: a refused write is logged and answered False.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, get_args

from app.config.database import admin_actions_collection
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: The fixed list of what an admin can do. Later batches add `report.review` and `content.remove`.
AdminAction = Literal[
    "invite.create",
    "invite.send",
    "invite.resend",
    "invite.revoke",
    "curation.review",
    "admin.grant",
    "admin.revoke",
    "bug.status",
]
ADMIN_ACTIONS: tuple = get_args(AdminAction)

#: The most characters a title or a reason keeps.
TEXT_MAX: int = 500


def _cut(text: Optional[str]) -> Optional[str]:
    return text[:TEXT_MAX] if isinstance(text, str) else text


def admin_action_row(
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    target_title: Optional[str] = None,
    reason: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The row one admin write leaves. Raises ValueError for an action outside the list."""
    if action not in ADMIN_ACTIONS:
        raise ValueError(f"unknown admin action: {action}")
    return {
        "actor_id": actor_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "target_title": _cut(target_title),
        "reason": _cut(reason),
        "details": details,
        "created_at": now or datetime.now(timezone.utc),
    }


async def record_admin_action(
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    target_title: Optional[str] = None,
    reason: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    collection: Any = None,
    now: Optional[datetime] = None,
) -> bool:
    """Append one row to the admin log. True when written; never raises."""
    target = admin_actions_collection if collection is None else collection
    try:
        row = admin_action_row(actor_id, action, target_type, target_id, target_title, reason, details, now)
        await target.insert_one(row)
        return True
    except Exception as exc:  # noqa: BLE001 - the log must never undo the admin write it records
        logger.warning("admin log: could not record %s on %s: %s", action, target_type, exc)
        return False
