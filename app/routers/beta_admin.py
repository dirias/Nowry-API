"""
The operator's calls for personal invitations (ADR-077 and its amendment, FR-012).

    POST /beta/admin/invites               invite one address; the answer carries the link, once (suite, scripts)
    POST /beta/admin/invites/batch         up to 50 addresses: checked (dry_run), then invited and emailed
    GET  /beta/admin/invites               the invitations, newest first, with counts per cohort and in all
    GET  /beta/admin/cohorts               what the paste form offers, and today's invitations left
    POST /beta/admin/invites/{id}/revoke   close an open invitation

All are admin-only; every write also needs a sign-in less than 12 hours old
(ADR-086 point 5) and leaves a row in the admin log. A batch dry run is a read.
The token appears in the single-address answer and in the email, nowhere else.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.dependencies import ensure_recent_admin_sign_in, require_admin, require_recent_admin
from app.config.beta import configured_cohorts
from app.config.invites import STATES, invite_link, invite_state
from app.models.Beta import (
    InviteBatchRequest,
    InviteBatchResponse,
    InviteCohortsResponse,
    InviteCreate,
    InviteCreated,
    InviteListResponse,
    InviteState,
    InviteSummary,
)
from app.services import beta_invites, invite_batch
from app.services.admin_actions import record_admin_action

router = APIRouter(prefix="/beta/admin", tags=["beta-admin"])

#: The cohort the waitlist's first invitations go to, offered by default (D11).
DEFAULT_COHORT = "c1-waitlist"
LIFETIMES = [7, 14, 30, 60, 90]
DEFAULT_LIFETIME = 14


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
        "locale": row.get("locale"),
        "sent_at": row.get("sent_at"),
        "send_count": int(row.get("send_count") or 0),
        "last_error": row.get("last_error"),
    }


def state_totals(counts: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """Every invitation by state, summed over the cohorts."""
    return {state: sum(per_cohort.get(state, 0) for per_cohort in counts.values()) for state in STATES}


@router.post("/invites", response_model=InviteCreated, status_code=status.HTTP_201_CREATED)
async def create_invite(payload: InviteCreate, admin: dict = Depends(require_recent_admin)) -> InviteCreated:
    """Invite one address and answer the link. An open invitation it already had is revoked. No email."""
    row, token = await beta_invites.create_invite(
        payload.email, payload.cohort, admin["user_id"], payload.expires_in_days, payload.note
    )
    await record_admin_action(admin["user_id"], "invite.create", "invite", str(row["_id"]), row["email"], details={"cohort": row["cohort"]})
    return InviteCreated(**summary_fields(row), link=invite_link(token))


@router.post("/invites/batch", response_model=InviteBatchResponse)
async def batch_invites(payload: InviteBatchRequest, admin: dict = Depends(require_admin)) -> InviteBatchResponse:
    """Check (dry run) or invite up to 50 addresses. A send needs the recent sign-in and today's budget."""
    if not payload.dry_run:
        ensure_recent_admin_sign_in(admin)
    answer = await invite_batch.run_batch(
        payload.addresses, payload.cohort, payload.expires_in_days, payload.locale, payload.dry_run, admin["user_id"]
    )
    return InviteBatchResponse(**answer)


@router.get("/invites", response_model=InviteListResponse)
async def list_invites(
    state: Optional[InviteState] = None,
    cohort: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=beta_invites.LIST_MAX),
    admin: dict = Depends(require_admin),
) -> InviteListResponse:
    """The invitations, newest first, and how many each cohort, and all of them, have in each state."""
    rows = await beta_invites.list_invites(state, cohort, limit)
    counts = await beta_invites.count_invites_by_cohort()
    return InviteListResponse(
        invites=[InviteSummary(**summary_fields(row)) for row in rows], counts=counts, totals=state_totals(counts)
    )


@router.get("/cohorts", response_model=InviteCohortsResponse)
async def list_cohorts(admin: dict = Depends(require_admin)) -> InviteCohortsResponse:
    """The cohorts in use (invitations, configured codes, the waitlist's), the lifetimes, and today's invitations left."""
    in_use = set(await beta_invites.count_invites_by_cohort()) | set(configured_cohorts()) | {DEFAULT_COHORT}
    return InviteCohortsResponse(
        cohorts=sorted(in_use),
        default_cohort=DEFAULT_COHORT,
        lifetimes=LIFETIMES,
        default_lifetime=DEFAULT_LIFETIME,
        invitations_left_today=await invite_batch.invitations_left_today(),
    )


@router.post("/invites/{invite_id}/revoke", response_model=InviteSummary)
async def revoke_invite(invite_id: str, admin: dict = Depends(require_recent_admin)) -> InviteSummary:
    """Close an open invitation. A used one is answered as it is, unchanged."""
    row = await beta_invites.revoke_invite(invite_id, admin["user_id"])
    if row is None:
        raise HTTPException(status_code=404, detail="No such invitation.")
    await record_admin_action(admin["user_id"], "invite.revoke", "invite", invite_id, row["email"])
    return InviteSummary(**summary_fields(row))
