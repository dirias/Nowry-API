"""
Resend and Invite again (INVITE-009, ADR-077 amendment, docs/prd-admin.md FR-012, D4).

One operation for both buttons: an open or expired row is "Resend", a revoked
row is "Invite again". It revokes the address's open invitation (if any), makes
a new one with the same address, cohort, language and lifetime, emails it and
logs `invite.resend`. The old link stops working the moment the new one exists.

Refusals, in this order: no such row (None, the route's 404); a used row
(409 `invite_used`); a letter to this address less than an hour ago (429
`resend_too_soon` with `retry_at`); no invitation left today (409 `over_budget`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.config import invites as rules
from app.config.database import invites_collection
from app.services import beta_invites, invitation_email, invite_batch
from app.services.admin_actions import record_admin_action

INVITE_USED_CODE = "invite_used"
RESEND_TOO_SOON_CODE = "resend_too_soon"
#: At most one letter an hour to one address.
RESEND_WINDOW = timedelta(hours=1)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def last_letter_at(row: Dict[str, Any]) -> Optional[datetime]:
    """When this row's link last reached the address, as far as the API knows.

    A letter that left: `sent_at`. A row made by the single-address call, whose
    link was handed over by hand: its creation. A row whose letter never left
    (`last_error`, no `sent_at`): nothing reached anyone, so it does not hold a resend back.
    """
    if row.get("sent_at"):
        return _aware(row["sent_at"])
    if row.get("last_error"):
        return None
    return _aware(row["created_at"])


def resend_opens_at(newest: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """When the next letter to this address may leave; None when it may now."""
    last = last_letter_at(newest) if newest else None
    return last + RESEND_WINDOW if last else None


async def newest_invitation(address: str, invites: Any) -> Optional[Dict[str, Any]]:
    rows = await invites.find({"email": address}).sort("created_at", -1).limit(1).to_list(length=1)
    return rows[0] if rows else None


async def check_resend(row: Dict[str, Any], invites: Any, now: datetime) -> None:
    """Raise the refusal a resend of this row meets, if any."""
    if rules.invite_state(row, now) == rules.STATUS_USED:
        raise HTTPException(status_code=409, detail={"code": INVITE_USED_CODE})
    opens = resend_opens_at(await newest_invitation(row["email"], invites))
    if opens is not None and now < opens:
        raise HTTPException(status_code=429, detail={"code": RESEND_TOO_SOON_CODE, "retry_at": opens.isoformat()})
    if invite_batch.spends_budget(row["email"]):
        invite_batch.refuse_over_budget(1, await invite_batch.invitations_left_today())


async def resend_invite(invite_id: str, admin_id: str, invites: Any = None, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Replace and email one invitation; the new row, or None when there is no such invitation."""
    invites = invites_collection if invites is None else invites
    at = now or datetime.now(timezone.utc)
    if not ObjectId.is_valid(invite_id):
        return None
    row = await invites.find_one({"_id": ObjectId(invite_id)})
    if row is None:
        return None
    await check_resend(row, invites, at)
    days = row.get("lifetime_days") or rules.DEFAULT_TTL_DAYS
    fresh, token = await beta_invites.create_invite(
        row["email"], row["cohort"], admin_id, days, row.get("note"), collection=invites, now=at, locale=row.get("locale") or "en",
    )
    fresh, outcome = await invitation_email.send_invitation(fresh, token, invites=invites, now=at)
    await record_admin_action(
        admin_id, "invite.resend", "invite", str(fresh["_id"]), row["email"],
        details={"replaces": invite_id, "state_before": rules.invite_state(row, at), "cohort": row["cohort"], "outcome": outcome},
    )
    return fresh
