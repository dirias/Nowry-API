"""
The admin area's own reads (ADMIN-007c, docs/prd-admin.md FR-011, boards A, K).

    GET /admin/overview   what needs the admin, today's email meters, the last 7 days of the log
    GET /admin/actions    the admin log, newest first, by kind, a page at a time

Both are admin-only reads (`require_admin`); neither writes, so neither needs
the recent sign-in.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import require_admin
from app.models.admin import ActionKind, ActionsPage, OverviewResponse
from app.services import admin_log, admin_overview

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(admin: dict = Depends(require_admin)) -> OverviewResponse:
    """The Overview tab in one answer."""
    return OverviewResponse(**await admin_overview.overview(admin.get("user_id")))


@router.get("/actions", response_model=ActionsPage)
async def list_actions(
    kind: Optional[ActionKind] = Query(default=None),
    before: Optional[str] = Query(
        default=None, max_length=80,
        description='A page\'s `next_before` ("<ISO instant>~<id>"), or a plain ISO instant: everything before it',
    ),
    limit: int = Query(default=admin_log.DEFAULT_LIMIT, ge=1, le=admin_log.MAX_LIMIT),
    admin: dict = Depends(require_admin),
) -> ActionsPage:
    """One page of the admin log; pass `next_before` back as `before` for the next."""
    try:
        page = await admin_log.read_actions(kind, before, limit, admin.get("user_id"))
    except admin_log.BadCursor:
        raise HTTPException(status_code=422, detail={"code": "bad_cursor"})
    return ActionsPage(**page)
