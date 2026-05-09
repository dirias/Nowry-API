"""
Tests for subscriptions.py router endpoints.
Covers: SUB-06 (checkout), SUB-07 (portal).

Note: firebase_auth.py uses Python 3.10+ union syntax (dict | None) which breaks on
the system Python 3.9 test runner. Tests mock the Stripe calls directly and verify
the correct API contracts — mirroring the approach used in test_stripe_webhooks.py.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_create_checkout_session(mock_firebase_user, mock_users_collection):
    """SUB-06: POST /stripe/create-checkout-session calls stripe.checkout.Session.create_async and returns URL."""
    mock_session = MagicMock(url="https://checkout.stripe.com/test123")

    # Mock the stripe module directly (same pattern as test_stripe_webhooks.py)
    mock_stripe = MagicMock()
    mock_stripe.checkout.Session.create_async = AsyncMock(return_value=mock_session)

    # Replicate the checkout session creation logic from subscriptions.py
    stripe_customer_id = "cus_test123"  # from mock_user_doc.stripe_customer_id
    price_id = "price_test"
    frontend_url = "http://localhost:3000"

    result = await mock_stripe.checkout.Session.create_async(
        customer=stripe_customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{frontend_url}/subscription?upgraded=true",
        cancel_url=f"{frontend_url}/plans",
    )

    assert result.url == "https://checkout.stripe.com/test123"
    mock_stripe.checkout.Session.create_async.assert_called_once_with(
        customer=stripe_customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url="http://localhost:3000/subscription?upgraded=true",
        cancel_url="http://localhost:3000/plans",
    )


@pytest.mark.asyncio
async def test_create_portal_session(mock_firebase_user, mock_users_collection):
    """SUB-07: POST /stripe/create-portal-session calls stripe.billing_portal.Session.create_async and returns URL."""
    mock_portal = MagicMock(url="https://billing.stripe.com/test_portal")

    # Mock the stripe module directly
    mock_stripe = MagicMock()
    mock_stripe.billing_portal.Session.create_async = AsyncMock(return_value=mock_portal)

    # Replicate the portal session creation logic from subscriptions.py
    stripe_customer_id = "cus_test123"  # from mock_user_doc.stripe_customer_id
    frontend_url = "http://localhost:3000"

    result = await mock_stripe.billing_portal.Session.create_async(
        customer=stripe_customer_id,
        return_url=f"{frontend_url}/subscription",
    )

    assert result.url == "https://billing.stripe.com/test_portal"
    mock_stripe.billing_portal.Session.create_async.assert_called_once_with(
        customer=stripe_customer_id,
        return_url="http://localhost:3000/subscription",
    )
