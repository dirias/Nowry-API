"""
POST /contact — the public contact form (docs/prd-public-site.md D6, FR-7).

No auth: the whole point is that a visitor can write before signing up. The
address is rate-limited instead, and the message is stored, not mailed
(a notification is a later task). `store_contact_message` holds the logic
so it can be tested without the limiter or a request.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, status

from app.config.database import contact_messages_collection
from app.core.limiter import limiter
from app.models.Contact import ContactMessageCreate, ContactMessageResponse

router = APIRouter(prefix="/contact", tags=["contact"])

ACKNOWLEDGEMENT = "Thank you. Your message was received."


def contact_document(payload: ContactMessageCreate, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The document a message is stored as: the form's fields, a status, a time."""
    return {
        "name": payload.name.strip(),
        "email": payload.email.lower(),
        "message": payload.message.strip(),
        "locale": payload.locale,
        "page": payload.page,
        "status": "new",
        "created_at": now or datetime.now(timezone.utc),
    }


async def store_contact_message(payload: ContactMessageCreate, collection) -> ContactMessageResponse:
    """Insert one message and return the acknowledgement the form shows."""
    result = await collection.insert_one(contact_document(payload))
    return ContactMessageResponse(message_id=str(result.inserted_id), message=ACKNOWLEDGEMENT)


@router.post("", response_model=ContactMessageResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def submit_contact_message(request: Request, payload: ContactMessageCreate) -> ContactMessageResponse:
    """Store a message from the public contact form. No auth; 5 per minute per address."""
    return await store_contact_message(payload, contact_messages_collection)
