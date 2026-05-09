"""
Subscriptions Router
Handles Stripe Checkout and Customer Portal session creation, and subscription status reads.

Endpoints:
  POST /stripe/create-checkout-session  — SUB-06: initiate upgrade flow
  POST /stripe/create-portal-session    — SUB-07: open billing management portal
  GET  /stripe/subscription-status      — SUB-08: read current subscription state
"""

import os
import stripe
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, ConfigDict
from app.auth.firebase_auth import get_firebase_user
from app.config.database import users_collection
from bson import ObjectId

# Configure Stripe at module level (RESEARCH.md Pattern 1 — never inside a request handler)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

# VALID_PRICE_IDS — whitelist built from env vars at module load time (T-03-04-01 mitigation).
# Unknown price_id → 400 before any Stripe call, preventing session creation for
# non-existent or discounted prices.
VALID_PRICE_IDS: set = {
    os.getenv("STRIPE_PLUS_MONTHLY_PRICE_ID"),
    os.getenv("STRIPE_PLUS_ANNUAL_PRICE_ID"),
    os.getenv("STRIPE_PRO_MONTHLY_PRICE_ID"),
    os.getenv("STRIPE_PRO_ANNUAL_PRICE_ID"),
}

router = APIRouter(tags=["stripe"])


# ---------------------------------------------------------------------------
# Request / Response models (Pydantic v2)
# ---------------------------------------------------------------------------

class CreateCheckoutSessionRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price_id: str


class CheckoutSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str


class PortalSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str


class SubscriptionStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tier: str
    status: str
    ai_usage_count: int
    next_billing_date: str | None
    ai_usage_reset_date: str | None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/stripe/create-checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    body: CreateCheckoutSessionRequest,
    current_user: dict = Depends(get_firebase_user),
) -> CheckoutSessionResponse:
    """
    SUB-06: Create a Stripe Checkout session for upgrading to Plus or Pro.

    Price ID is validated against a server-side whitelist before any Stripe call
    (T-03-04-01 tamper mitigation). User can only create sessions for their own
    stripe_customer_id resolved from their Firebase UID (T-03-04-02).
    Stripe customer is created on-demand if the user has no stripe_customer_id
    (existing-user fallback — RESEARCH.md Pitfall 5).
    """
    # Price ID whitelist validation — BEFORE any MongoDB or Stripe call (security D-03)
    if body.price_id not in VALID_PRICE_IDS:
        raise HTTPException(status_code=400, detail="Invalid price ID")

    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Existing user fallback (Pitfall 5): create Stripe customer on-demand if missing
    stripe_customer_id = user.get("stripe_customer_id")
    if not stripe_customer_id:
        customer = await stripe.Customer.create_async(
            email=user.get("email"),
            metadata={
                "firebase_uid": user.get("firebase_uid"),
                "user_id": str(user["_id"]),
            },
        )
        stripe_customer_id = customer.id
        await users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"stripe_customer_id": stripe_customer_id}},
        )

    session = await stripe.checkout.Session.create_async(
        customer=stripe_customer_id,
        mode="subscription",
        line_items=[{"price": body.price_id, "quantity": 1}],
        success_url=f"{FRONTEND_URL}/subscription?upgraded=true",
        cancel_url=f"{FRONTEND_URL}/plans",
    )
    return CheckoutSessionResponse(url=session.url)


@router.post("/stripe/create-portal-session", response_model=PortalSessionResponse)
async def create_portal_session(
    current_user: dict = Depends(get_firebase_user),
) -> PortalSessionResponse:
    """
    SUB-07: Create a Stripe Customer Portal session for billing management.

    Requires the user to already have a stripe_customer_id (i.e. has previously
    subscribed). Returns 400 if no billing account is found.
    """
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    stripe_customer_id = user.get("stripe_customer_id")
    if not stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="No billing account found. Please upgrade first.",
        )

    portal_session = await stripe.billing_portal.Session.create_async(
        customer=stripe_customer_id,
        return_url=f"{FRONTEND_URL}/subscription",
    )
    return PortalSessionResponse(url=portal_session.url)


@router.get("/stripe/subscription-status", response_model=SubscriptionStatusResponse)
async def get_subscription_status(
    current_user: dict = Depends(get_firebase_user),
) -> SubscriptionStatusResponse:
    """
    SUB-08: Read current subscription state from MongoDB.

    No live Stripe call is made per D-12 — subscription state is always kept in sync
    via the webhook handler (stripe_webhooks.py). This endpoint is a fast read-only path.
    """
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)}, {"subscription": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    sub = user.get("subscription", {})
    return SubscriptionStatusResponse(
        tier=sub.get("tier", "free"),
        status=sub.get("status", "active"),
        ai_usage_count=sub.get("ai_usage_count", 0),
        next_billing_date=(
            str(sub["next_billing_date"]) if sub.get("next_billing_date") else None
        ),
        ai_usage_reset_date=(
            str(sub["ai_usage_reset_date"]) if sub.get("ai_usage_reset_date") else None
        ),
    )
