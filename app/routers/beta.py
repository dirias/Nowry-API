"""
The beta gate's public endpoints (ADR-038, docs/prd-road-to-market.md FR-001, FR-002, FR-004).

    GET  /beta/config          the three flags, read once by every client
    POST /beta/invites/check   can this code still make an account
    POST /beta/waitlist        one row per email for the next cohort

No auth on any of them: the config decides what a visitor sees, an invite is
checked before an account exists, and the waitlist is for people who were not
let in. The two writes are rate-limited like `/contact`. The enforcement of
the invite is not here; it is where accounts are created (`firebase_auth`).

A first submission to the waitlist tells the operator's mailbox (WAIT-001,
FR-013); a repeat tells no one. The visitor gets the same answer either way,
so the form cannot be used to learn whether an address is on the list.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Request, status
from pymongo import ReturnDocument

from app.config.beta import beta_config, open_code_rule
from app.config.database import waitlist_collection
from app.core.limiter import limiter
from app.models.Beta import (
    BetaConfigResponse,
    InviteCheckRequest,
    InviteCheckResponse,
    WaitlistCreate,
    WaitlistResponse,
)
from app.services.beta_invites import count_code_uses
from app.services.notifications import notify, waitlist_notification

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


async def upsert_waitlist(payload: WaitlistCreate, collection, now: Optional[datetime] = None) -> Tuple[Dict[str, Any], bool]:
    """Upsert by email so a second submission updates rather than duplicates.

    Returns the row's fields and whether the email is new. The update hands back
    the row as it was before the write: none means this was the first time.
    """
    doc = waitlist_document(payload, now)
    before = await collection.find_one_and_update(
        {"email": doc["email"]},
        {"$set": doc, "$setOnInsert": {"created_at": doc["updated_at"]}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    return doc, before is None


async def join_waitlist(payload: WaitlistCreate, collection, now: Optional[datetime] = None) -> WaitlistResponse:
    """Store the email and return the one acknowledgement every submission gets."""
    await upsert_waitlist(payload, collection, now)
    return WaitlistResponse(message=WAITLIST_ACK)


@router.get("/config", response_model=BetaConfigResponse)
async def get_beta_config() -> BetaConfigResponse:
    """The three flags. Unset env means no beta."""
    return BetaConfigResponse(**beta_config())


@router.post("/invites/check", response_model=InviteCheckResponse)
@limiter.limit("10/minute")
async def check_invite(request: Request, payload: InviteCheckRequest) -> InviteCheckResponse:
    """Whether a code can still make an account. The register form asks before creating a Firebase account.

    An unknown, full or expired code gets the same answer, and the cohort is
    not named: the endpoint is public (ADR-077, FR-020).
    """
    rule = await open_code_rule(payload.code, count_code_uses)
    return InviteCheckResponse(valid=rule is not None)


@router.post("/waitlist", response_model=WaitlistResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def submit_waitlist(request: Request, payload: WaitlistCreate, background_tasks: BackgroundTasks) -> WaitlistResponse:
    """Store an email for the next cohort; tell the mailbox when it is a new one. No auth; 5 per minute per address."""
    doc, is_new = await upsert_waitlist(payload, waitlist_collection)
    if is_new:
        background_tasks.add_task(notify, *waitlist_notification(doc))
    return WaitlistResponse(message=WAITLIST_ACK)
