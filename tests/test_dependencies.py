"""
Tests for auth/dependencies.py additions.
Covers: SUB-05 (get_subscription_tier), SUB-11 (track_ai_usage).
All tests fail until dependencies.py is patched (Wave 1 Plan 03-04).
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_get_subscription_tier(mock_firebase_user, mock_users_collection):
    """SUB-05: get_subscription_tier returns 'free' for a free-tier user doc."""
    pytest.fail("Not implemented — Wave 1 Plan 03-04 must add get_subscription_tier to dependencies.py")


@pytest.mark.asyncio
async def test_track_ai_usage(mock_firebase_user, mock_users_collection):
    """SUB-11: track_ai_usage increments subscription.ai_usage_count by 1 in MongoDB."""
    pytest.fail("Not implemented — Wave 1 Plan 03-04 must add track_ai_usage to dependencies.py")
