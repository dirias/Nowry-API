"""
The operator's calls for personal invitations (ADR-077, FR-019).

    POST /beta/admin/invites               invite one address; the answer carries the link, once
    GET  /beta/admin/invites               the invitations, newest first, with counts per cohort
    POST /beta/admin/invites/{id}/revoke   close an open invitation

All three are admin-only. None sends an email: the link is sent by hand until
WAIT-006. The token appears in the first answer and nowhere else, logs included.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.dependencies import require_admin, require_recent_admin
from app.config.invites import invite_link, invite_state
from app.models.Beta import InviteCreate, InviteCreated, InviteListResponse, InviteState, InviteSummary
from app.services import beta_invites
from app.services.admin_actions import record_admin_action

router = APIRouter(prefix="/beta/admin", tags=["beta-admin"])


def summary_fields(row: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """A stored row as the operator reads it: its state worked out, its hash left behind."""
    return {
        "id": str(row["_id"]),
        "email": row["email"],
        "cohort": row["cohort"],
        "state": invite_state(row, now or datetime.now(timezone.utc)),
        "note": row.get("note"),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "used_at": row.get("used_at"),
        "revoked_at": row.get("revoked_at"),
    }


@router.post("/invites", response_model=InviteCreated, status_code=status.HTTP_201_CREATED)
async def create_invite(payload: InviteCreate, admin: dict = Depends(require_recent_admin)) -> InviteCreated:
    """Invite one address. An open invitation it already had is revoked."""
    row, token = await beta_invites.create_invite(
        payload.email, payload.cohort, admin["user_id"], payload.expires_in_days, payload.note
    )
    await record_admin_action(admin["user_id"], "invite.create", "invite", str(row["_id"]), row["email"], details={"cohort": row["cohort"]})
    return InviteCreated(**summary_fields(row), link=invite_link(token))


@router.get("/invites", response_model=InviteListResponse)
async def list_invites(
    state: Optional[InviteState] = None,
    cohort: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=beta_invites.LIST_MAX),
    admin: dict = Depends(require_admin),
) -> InviteListResponse:
    """The invitations, newest first, and how many each cohort has in each state."""
    rows = await beta_invites.list_invites(state, cohort, limit)
    counts = await beta_invites.count_invites_by_cohort()
    return InviteListResponse(invites=[InviteSummary(**summary_fields(row)) for row in rows], counts=counts)


@router.post("/invites/{invite_id}/revoke", response_model=InviteSummary)
async def revoke_invite(invite_id: str, admin: dict = Depends(require_recent_admin)) -> InviteSummary:
    """Close an open invitation. A used one is answered as it is, unchanged."""
    row = await beta_invites.revoke_invite(invite_id, admin["user_id"])
    if row is None:
        raise HTTPException(status_code=404, detail="No such invitation.")
    await record_admin_action(admin["user_id"], "invite.revoke", "invite", invite_id, row["email"])
    return InviteSummary(**summary_fields(row))
