"""
The beta gate's public endpoints (ADR-038, docs/prd-road-to-market.md FR-001, FR-002, FR-004).

    GET  /beta/config          the three flags, read once by every client
    POST /beta/invites/check   can this code still make an account
    POST /beta/waitlist        one row per email for the next cohort
    GET  /beta/waitlist/leave/{token}   what the leave page shows; changes nothing
    POST /beta/waitlist/leave/{token}   leave the list (the page's key, and a mail provider's one click)

No auth on any of them: the config decides what a visitor sees, an invite is
checked before an account exists, and the waitlist is for people who were not
let in. The two writes are rate-limited like `/contact`. The enforcement of
the invite is not here; it is where accounts are created (`firebase_auth`).

The waitlist form answers every address alike before anything is looked up
(ADR-084, FR-034); after the answer, an address with an account is sent a
letter and not added, a new one tells the operator's mailbox (WAIT-001,
FR-013), and each is sent its one confirmation if it is owed one (WAIT-004,
FR-027).

Leaving (MAIL-008, FR-026, ADR-080): reading a token removes no one, since
mail scanners open links; only the POST does, and it answers the same for any
token.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, status

from app.config.beta import beta_config, open_code_rule
from app.config.database import users_collection, waitlist_collection
from app.core.limiter import limiter
from app.models.Beta import (
    BetaConfigResponse,
    InviteCheckRequest,
    InviteCheckResponse,
    WaitlistCreate,
    WaitlistLeaveHint,
    WaitlistLeft,
    WaitlistResponse,
)
from app.config.invites import is_personal_token
from app.services.beta_invites import count_code_uses, personal_invite_usable
from app.services.notifications import notify, waitlist_notification
from app.services import waitlist
from app.services.account_lookup import active_account_by_email
from app.utils.logger import get_logger
from app.services.waitlist import join as upsert_waitlist, waitlist_document  # noqa: F401 - the names earlier callers import

router = APIRouter(prefix="/beta", tags=["beta"])
logger = get_logger(__name__)

# ADR-084: one answer for every address; the clients show their own translation of it.
WAITLIST_ACK = "Done. If this is your first time, we've sent you a confirmation email. If you were already on the list, you're still on it; nothing else to do."


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
    if is_personal_token(payload.code):
        # ADR-077: usable or not, and nothing else. No address is taken or given.
        return InviteCheckResponse(valid=await personal_invite_usable(payload.code))
    rule = await open_code_rule(payload.code, count_code_uses)
    return InviteCheckResponse(valid=rule is not None)


@router.post("/waitlist", response_model=WaitlistResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def submit_waitlist(request: Request, payload: WaitlistCreate, background_tasks: BackgroundTasks) -> WaitlistResponse:
    """Store an email for the next cohort; tell the mailbox when it is a new one. No auth; 5 per minute per address."""
    # ADR-084: nothing is looked up or written before the answer, so every
    # address gets the same answer at the same point.
    background_tasks.add_task(after_waitlist_form, payload)
    return WaitlistResponse(message=WAITLIST_ACK)


async def after_waitlist_form(payload: WaitlistCreate, collection=None, users=None) -> None:
    """What a submission does once it is answered (ADR-084, FR-034). Never raises.

    An address with an active account is told so and not added. Any other is
    upserted; a new one tells the operator; every one asks for its confirmation,
    which the row grants once per address, ever (FR-027).
    """
    collection = waitlist_collection if collection is None else collection
    users = users_collection if users is None else users
    try:
        if await active_account_by_email(users, payload.email):
            await waitlist.send_account_letter(payload.email, payload.locale)
            return
        doc, is_new = await upsert_waitlist(payload, collection)
        if is_new:
            await notify(*waitlist_notification(doc))
        await waitlist.send_confirmation(doc["email"], collection)
    except Exception as exc:  # noqa: BLE001 - the visitor already has their answer
        logger.warning("waitlist: a submission could not be completed: %s", type(exc).__name__)


@router.get("/waitlist/leave/{token}", response_model=WaitlistLeaveHint)
@limiter.limit("30/minute")
async def read_waitlist_leave(request: Request, token: str) -> WaitlistLeaveHint:
    """What the leave page shows before anything changes. An unknown token answers with nothing."""
    hint = await waitlist.leave_hint(token, waitlist_collection)
    return WaitlistLeaveHint(**(hint or {}))


@router.post("/waitlist/leave/{token}", response_model=WaitlistLeft)
@limiter.limit("30/minute")
async def leave_waitlist(request: Request, token: str) -> WaitlistLeft:
    """Leave the list. The same answer for a known, an unknown and a used token; the body is not read."""
    await waitlist.leave(token, waitlist_collection)
    return WaitlistLeft()
