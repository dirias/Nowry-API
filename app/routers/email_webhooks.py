"""
Resend tells the API what happened to an email (MAIL-008, FR-026, ADR-080 point 5).

    POST /webhooks/resend

Two events matter: a permanent bounce and a complaint. Either marks the
address so no letter to a visitor is sent to it again. Everything else is
answered and ignored.

Resend signs each request the way Svix does: the id, the timestamp and the raw
body, joined by dots, under HMAC-SHA256 with the secret's base64 part
(`RESEND_WEBHOOK_SECRET`, shown as `whsec_…` in Resend's dashboard). A request
with no valid signature, or one older than five minutes, is refused, and so is
every request while the secret is unset.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request

from app.config.database import waitlist_collection
from app.services import waitlist
from app.services.email_suppressions import BOUNCED, COMPLAINED
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

TOLERANCE_SECONDS = 300
SECRET_PREFIX = "whsec_"


def expected_signature(secret: str, message_id: str, timestamp: str, body: bytes) -> Optional[str]:
    """The base64 signature Resend would send for this request, or None when the secret cannot be read."""
    try:
        key = base64.b64decode(secret.strip().removeprefix(SECRET_PREFIX))
    except Exception:  # noqa: BLE001 - an unreadable secret verifies nothing
        return None
    if not key:
        return None
    signed = f"{message_id}.{timestamp}.".encode("utf-8") + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


def verified(secret: str, message_id: str, timestamp: str, signatures: str, body: bytes, now: Optional[float] = None) -> bool:
    """True when one of the header's `v1,<signature>` entries matches and the request is recent."""
    if not (secret and message_id and timestamp and signatures):
        return False
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > TOLERANCE_SECONDS:
        return False
    expected = expected_signature(secret, message_id, timestamp, body)
    if expected is None:
        return False
    for entry in signatures.split():
        version, _, value = entry.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def suppression_reason(event: dict) -> Optional[str]:
    """`bounced` for a permanent bounce, `complained` for a complaint, None for anything else."""
    kind = event.get("type")
    if kind == "email.complained":
        return COMPLAINED
    if kind == "email.bounced":
        bounce = (event.get("data") or {}).get("bounce") or {}
        # A bounce that does not say what it is counts as permanent; a transient one is a full mailbox.
        return BOUNCED if str(bounce.get("type") or "Permanent").lower() == "permanent" else None
    return None


def recipients(event: dict) -> List[str]:
    to: Any = (event.get("data") or {}).get("to")
    if isinstance(to, str):
        to = [to]
    return [address for address in (to or []) if isinstance(address, str) and "@" in address][:10]


@router.post("/resend")
async def resend_webhook(request: Request) -> dict:
    """Mark the addresses of a permanent bounce or a complaint. No auth but the signature."""
    body = await request.body()
    secret = (os.getenv("RESEND_WEBHOOK_SECRET") or "").strip()
    headers = request.headers
    if not verified(secret, headers.get("svix-id", ""), headers.get("svix-timestamp", ""), headers.get("svix-signature", ""), body):
        raise HTTPException(status_code=401, detail="Invalid signature.")
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid body.")
    reason = suppression_reason(event) if isinstance(event, dict) else None
    if reason:
        for address in recipients(event):
            await waitlist.mark_suppressed(address, reason, waitlist_collection)
        logger.info("resend webhook: %s marked %d address(es)", reason, len(recipients(event)))
    return {"received": True}
