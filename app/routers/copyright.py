"""
POST /copyright-notices — the takedown form (GTM-008, ADR-037's missing half).

No auth: a rights holder is not a user. The address is rate-limited instead
and the notice is stored, the way the contact form is; the notification to
the mailbox arrives with GTM-007. `store_copyright_notice` holds the logic so
it is tested without the limiter or a request.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, status

from app.config.database import copyright_notices_collection
from app.core.limiter import limiter
from app.models.CopyrightNotice import CopyrightNoticeCreate, CopyrightNoticeResponse

router = APIRouter(prefix="/copyright-notices", tags=["copyright"])

ACKNOWLEDGEMENT = "Your notice was received."


def notice_document(payload: CopyrightNoticeCreate, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The document a notice is stored as: the form's fields, a status, a time."""
    return {
        "name": payload.name.strip(),
        "email": payload.email.lower(),
        "work": payload.work.strip(),
        "location": payload.location.strip(),
        "signature": payload.signature.strip(),
        "locale": payload.locale,
        "status": "new",
        "created_at": now or datetime.now(timezone.utc),
    }


async def store_copyright_notice(payload: CopyrightNoticeCreate, collection) -> CopyrightNoticeResponse:
    """Insert one notice and return the acknowledgement the form shows."""
    result = await collection.insert_one(notice_document(payload))
    return CopyrightNoticeResponse(notice_id=str(result.inserted_id), message=ACKNOWLEDGEMENT)


@router.post("", response_model=CopyrightNoticeResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def submit_copyright_notice(request: Request, payload: CopyrightNoticeCreate) -> CopyrightNoticeResponse:
    """Store a notice from the takedown page. No auth; 5 per minute per address."""
    return await store_copyright_notice(payload, copyright_notices_collection)
