"""
Tests for subscriptions.py router endpoints.
Covers: SUB-06 (checkout), SUB-07 (portal).
All tests fail until subscriptions.py is implemented (Wave 1 Plan 03-04).
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_create_checkout_session(mock_firebase_user, mock_users_collection):
    """SUB-06: POST /stripe/create-checkout-session returns a Stripe Checkout URL."""
    pytest.fail("Not implemented — Wave 1 Plan 03-04 must implement subscriptions.py")


@pytest.mark.asyncio
async def test_create_portal_session(mock_firebase_user, mock_users_collection):
    """SUB-07: POST /stripe/create-portal-session returns a Stripe Customer Portal URL."""
    pytest.fail("Not implemented — Wave 1 Plan 03-04 must implement subscriptions.py")
