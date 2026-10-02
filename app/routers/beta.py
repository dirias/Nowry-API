"""
The beta gate's public endpoints (ADR-038, docs/prd-road-to-market.md FR-001, FR-002, FR-004).

    GET  /beta/config          the three flags, read once by every client
    POST /beta/invites/check   is this code one of ours, and which cohort
    POST /beta/waitlist        one row per email for the next cohort

No auth on any of them: the config decides what a visitor sees, an invite is
checked before an account exists, and the waitlist is for people who were not
let in. The two writes are rate-limited like `/contact`. The enforcement of
the invite is not here; it is where accounts are created (`firebase_auth`).
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, status
from pymongo import ReturnDocument

from app.config.beta import beta_config, invite_cohort
from app.config.database import waitlist_collection
from app.core.limiter import limiter
from app.models.Beta import (
    BetaConfigResponse,
    InviteCheckRequest,
    InviteCheckResponse,
    WaitlistCreate,
    WaitlistResponse,
)

router = APIRouter(prefix="/beta", tags=["beta"])

WAITLIST_ACK = "You're on the list. We'll write when a place opens."


def waitlist_document(payload: WaitlistCreate, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The fields an upsert sets: the form's, lowercased email, a status, times."""
    at = now or datetime.now(timezone.utc)
    return {
        "email": payload.email.lower(),
        "locale": payload.locale,
        "source": payload.source,
        "status": "waiting",
        "updated_at": at,
    }


async def join_waitlist(payload: WaitlistCreate, collection, now: Optional[datetime] = None) -> WaitlistResponse:
    """Upsert by email so a second submission updates rather than duplicates."""
    doc = waitlist_document(payload, now)
    await collection.find_one_and_update(
        {"email": doc["email"]},
        {"$set": doc, "$setOnInsert": {"created_at": doc["updated_at"]}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return WaitlistResponse(message=WAITLIST_ACK)


@router.get("/config", response_model=BetaConfigResponse)
async def get_beta_config() -> BetaConfigResponse:
    """The three flags. Unset env means no beta."""
    return BetaConfigResponse(**beta_config())


@router.post("/invites/check", response_model=InviteCheckResponse)
@limiter.limit("10/minute")
async def check_invite(request: Request, payload: InviteCheckRequest) -> InviteCheckResponse:
    """Whether a code is one of ours. The register form asks before creating a Firebase account."""
    cohort = invite_cohort(payload.code)
    return InviteCheckResponse(valid=cohort is not None, cohort=cohort)


@router.post("/waitlist", response_model=WaitlistResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def submit_waitlist(request: Request, payload: WaitlistCreate) -> WaitlistResponse:
    """Store an email for the next cohort. No auth; 5 per minute per address."""
    return await join_waitlist(payload, waitlist_collection)
