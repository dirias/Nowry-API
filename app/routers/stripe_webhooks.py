"""
Stripe webhook handler for subscription lifecycle events.

Handles: customer.subscription.updated, customer.subscription.deleted,
         invoice.payment_failed, invoice.payment_succeeded

Security (per threat model T-03-03-01 through T-03-03-05):
- HMAC-SHA256 signature verification via Webhook.construct_event()
- Idempotency via stripe_processed_events_collection (unique stripe_event_id)
- No Firebase auth dependency — Stripe sends server-to-server with its own signature
- Unknown price IDs default to "free" — cannot escalate tier
"""

import os
import stripe
from fastapi import APIRouter, Request, HTTPException
from stripe import Webhook, SignatureVerificationError
from datetime import datetime, timezone
from typing import Optional

from app.config.database import users_collection, stripe_processed_events_collection
from app.config.subscription_plans import STRIPE_PRICE_IDS

# Configure Stripe API key once at module level — never per-request
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

WEBHOOK_SECRET: Optional[str] = os.getenv("STRIPE_WEBHOOK_SECRET")

# Router has no auth dependency — Stripe sends its own Stripe-Signature header
router = APIRouter(prefix="/stripe", tags=["stripe"])


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict:
    """
    Receive and process Stripe webhook events.

    CRITICAL: uses await request.body() for raw bytes — never request.json().
    Stripe signature verification operates on the exact byte sequence; any parsing
    or encoding conversion invalidates the HMAC signature check.
    """
    payload: bytes = await request.body()  # raw bytes — NEVER .json() or .decode()
    sig_header: Optional[str] = request.headers.get("stripe-signature")

    try:
        stripe_event = Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
        # Convert to plain dict so all handlers can use .get() safely.
        # StripeObject supports to_dict() in all SDK versions >= 5.
        event: dict = stripe_event.to_dict()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception:
        # Catches misconfiguration (e.g. WEBHOOK_SECRET=None) and malformed headers
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Idempotency: check if this event was already processed before mutating MongoDB
    stripe_event_id: str = event["id"]
    already_processed = await stripe_processed_events_collection.find_one(
        {"stripe_event_id": stripe_event_id}
    )
    if already_processed:
        return {"status": "already_processed"}

    # Mark as processed immediately (before mutation) to prevent duplicate processing
    # on concurrent deliveries (Stripe retries if no 200 received within 30s)
    await stripe_processed_events_collection.insert_one({
        "stripe_event_id": stripe_event_id,
        "event_type": event["type"],
        "processed_at": datetime.now(timezone.utc),
    })

    await _dispatch_event(event)
    return {"status": "ok"}


async def _dispatch_event(event: dict) -> None:
    """Route Stripe event to the appropriate handler. Unknown events are silently ignored."""
    event_type: str = event["type"]
    if event_type == "customer.subscription.updated":
        await _handle_subscription_updated(event)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(event)
    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(event)
    elif event_type == "invoice.payment_succeeded":
        await _handle_payment_succeeded(event)
    # All other event types silently ignored — no error, Stripe still receives 200


async def _handle_subscription_updated(event: dict) -> None:
    """
    Sync subscription tier and billing metadata from customer.subscription.updated.

    Maps the Stripe price ID to a tier via STRIPE_PRICE_IDS env vars.
    Unknown price IDs default to "free" — cannot be manipulated to grant a higher tier.
    """
    subscription: dict = event["data"]["object"]
    stripe_customer_id: str = subscription["customer"]
    status: str = subscription.get("status", "active")
    now: datetime = datetime.now(timezone.utc)

    # Derive billing details from subscription items
    items_data: list = subscription.get("items", {}).get("data", [])
    first_item: dict = items_data[0] if items_data else {}
    price_id: Optional[str] = first_item.get("price", {}).get("id")
    interval: Optional[str] = first_item.get("plan", {}).get("interval")

    current_period_end: Optional[int] = subscription.get("current_period_end")
    next_billing_date: Optional[datetime] = (
        datetime.fromtimestamp(current_period_end, tz=timezone.utc)
        if current_period_end else None
    )

    # Map price_id to tier via env vars — default to "free" for unknown price IDs
    tier: str = "free"
    if price_id in (STRIPE_PRICE_IDS.get("plus_monthly"), STRIPE_PRICE_IDS.get("plus_annual")):
        tier = "plus"
    elif price_id in (STRIPE_PRICE_IDS.get("pro_monthly"), STRIPE_PRICE_IDS.get("pro_annual")):
        tier = "pro"

    await users_collection.update_one(
        {"stripe_customer_id": stripe_customer_id},
        {"$set": {
            "subscription.tier": tier,
            "subscription.status": "active" if status == "active" else status,
            "subscription.next_billing_date": next_billing_date,
            "subscription.stripe_subscription_id": subscription.get("id"),
            "subscription.billing_interval": interval,
            "subscription.subscription_status_updated_at": now,
        }}
    )


async def _handle_subscription_deleted(event: dict) -> None:
    """
    Apply 7-day grace period check on customer.subscription.deleted.

    Grace period logic (D-11):
    - If subscription_status_updated_at exists AND is >= 7 days ago: downgrade to free.
    - If no subscription_status_updated_at (direct cancellation, not payment failure): downgrade immediately.
    - If subscription_status_updated_at is < 7 days ago: grace period still active, no action.
    """
    subscription: dict = event["data"]["object"]
    stripe_customer_id: str = subscription["customer"]
    now: datetime = datetime.now(timezone.utc)

    user = await users_collection.find_one({"stripe_customer_id": stripe_customer_id})
    if not user:
        return

    past_due_since: Optional[datetime] = (
        user.get("subscription", {}).get("subscription_status_updated_at")
    )

    should_downgrade: bool = False
    if past_due_since is None:
        # Direct cancellation without a payment failure phase — downgrade immediately
        should_downgrade = True
    else:
        # Ensure timezone-aware comparison
        if past_due_since.tzinfo is None:
            past_due_since = past_due_since.replace(tzinfo=timezone.utc)
        days_past_due: int = (now - past_due_since).days
        if days_past_due >= 7:
            # Grace period expired — downgrade to free
            should_downgrade = True
        # else: grace period still active — do not downgrade yet

    if should_downgrade:
        await users_collection.update_one(
            {"stripe_customer_id": stripe_customer_id},
            {"$set": {
                "subscription.tier": "free",
                "subscription.status": "active",
                "subscription.next_billing_date": None,
                "subscription.stripe_subscription_id": None,
                "subscription.billing_interval": None,
                "subscription.subscription_status_updated_at": now,
            }}
        )


async def _handle_payment_failed(event: dict) -> None:
    """
    Set subscription status to past_due on invoice.payment_failed.

    Records subscription_status_updated_at to start the 7-day grace period clock.
    The customer.subscription.deleted handler reads this timestamp to determine
    whether to downgrade.
    """
    invoice: dict = event["data"]["object"]
    stripe_customer_id: str = invoice["customer"]
    now: datetime = datetime.now(timezone.utc)

    await users_collection.update_one(
        {"stripe_customer_id": stripe_customer_id},
        {"$set": {
            "subscription.status": "past_due",
            "subscription.subscription_status_updated_at": now,
        }}
    )


async def _handle_payment_succeeded(event: dict) -> None:
    """
    Reset AI usage counter on invoice.payment_succeeded (D-08, SUB-11).

    Resets ai_usage_count to 0 and sets ai_usage_reset_date to the next billing
    period end date (derived from the invoice line item period.end).
    Also restores subscription status to active (clears past_due if payment resolved).
    """
    invoice: dict = event["data"]["object"]
    stripe_customer_id: str = invoice["customer"]
    now: datetime = datetime.now(timezone.utc)

    # Derive next reset date from invoice line item period end
    lines_data: list = invoice.get("lines", {}).get("data", [])
    first_line: dict = lines_data[0] if lines_data else {}
    next_period_end: Optional[int] = first_line.get("period", {}).get("end")
    reset_date: datetime = (
        datetime.fromtimestamp(next_period_end, tz=timezone.utc)
        if next_period_end else now
    )

    await users_collection.update_one(
        {"stripe_customer_id": stripe_customer_id},
        {"$set": {
            "subscription.ai_usage_count": 0,
            "subscription.ai_usage_reset_date": reset_date,
            "subscription.status": "active",
            "subscription.subscription_status_updated_at": now,
        }}
    )
