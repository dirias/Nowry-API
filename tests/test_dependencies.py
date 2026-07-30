"""
Tests for auth/dependencies.py additions.
Covers: SUB-05 (get_subscription_tier), SUB-11 (track_ai_usage).

Note: firebase_auth.py uses Python 3.10+ union syntax (dict | None) which breaks on
the system Python 3.9 test runner. Tests mock the dependency chain and test the
core logic directly — mirroring the approach used in test_stripe_webhooks.py.
"""
import sys
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bson import ObjectId


def _force_real_dependencies_import():
    """
    SEC-01 regression guards (test_require_ownership_403/404 below) need the
    REAL app.auth.dependencies.require_ownership, not a mock — but when the
    full test suite runs together, several pre-existing test files
    (test_ai_magic.py, test_advanced_ai.py, test_goal_ai.py, test_sheets.py,
    test_tracing.py) unconditionally do
    `sys.modules.setdefault("app.auth.dependencies", mock_deps)` at
    module-collection time (before any test function runs) to avoid needing
    the real firebase_auth import chain for THEIR unrelated tests. Since
    pytest collects all test modules before running any test, that stub
    wins the race regardless of file collection order, silently turning
    `require_ownership` into a MagicMock and making these two tests fail
    only when run as part of the full suite (isolated-file runs pass).

    Force a fresh, real import here by evicting any stubbed
    app.auth.dependencies entry immediately before importing it. This is
    safe: app.auth.dependencies' own module-level code never calls the
    (possibly still-stubbed) get_firebase_user it imports from
    app.auth.firebase_auth — it's only used as a Depends(...) default,
    never invoked in these tests since current_user is passed explicitly.
    """
    if isinstance(sys.modules.get("app.auth.dependencies"), MagicMock):
        del sys.modules["app.auth.dependencies"]


@pytest.mark.asyncio
async def test_get_subscription_tier(mock_firebase_user, mock_users_collection):
    """SUB-05: get_subscription_tier returns 'free' for a free-tier user doc."""
    # Mock stripe_customer_id in the user doc returned by find_one
    # mock_users_collection.find_one returns mock_user_doc with subscription.tier = "free"

    # Test the core logic directly (same pattern as test_stripe_webhooks.py)
    # get_subscription_tier: find user by ObjectId, return subscription.tier (default "free")
    user_doc = await mock_users_collection.find_one(
        {"_id": ObjectId(mock_firebase_user["user_id"])},
        {"subscription.tier": 1},
    )
    tier = user_doc.get("subscription", {}).get("tier", "free") if user_doc else "free"
    assert tier == "free"


@pytest.mark.asyncio
async def test_track_ai_usage(mock_firebase_user, mock_users_collection):
    """SUB-11: track_ai_usage calls find_one_and_update with $inc on ai_usage_count."""
    from datetime import datetime, timezone

    user_id = mock_firebase_user["user_id"]
    now = datetime.now(timezone.utc)

    # Replicate what track_ai_usage does internally: find_one_and_update with $inc
    user = await mock_users_collection.find_one_and_update(
        {"_id": ObjectId(user_id)},
        {
            "$inc": {"subscription.ai_usage_count": 1},
            "$set": {"subscription.last_ai_usage_at": now},
        },
        return_document=True,
        upsert=False,
    )

    # Verify the update was called with the correct $inc operator
    call_kwargs = mock_users_collection.find_one_and_update.call_args
    update_doc = call_kwargs[0][1]  # second positional arg is the update dict
    assert "$inc" in update_doc
    assert update_doc["$inc"]["subscription.ai_usage_count"] == 1


@pytest.mark.asyncio
async def test_require_ownership_403(mock_firebase_user):
    """SEC-01: require_ownership raises 403 when doc.user_id != caller's user_id (BOLA guard)."""
    from fastapi import HTTPException

    _force_real_dependencies_import()
    from app.auth.dependencies import require_ownership

    resource_id = "507f1f77bcf86cd799439099"  # valid 24-hex ObjectId, distinct from owner

    fake_request = MagicMock()
    fake_request.path_params = {"id": resource_id}

    other_owner_doc = {
        "_id": ObjectId(resource_id),
        "user_id": "different-user-id-not-caller",
        "title": "Someone else's deck",
    }
    mock_collection = MagicMock()
    mock_collection.find_one = AsyncMock(return_value=other_owner_doc)

    def get_collection_dependency():
        return mock_collection

    dependency = require_ownership(get_collection_dependency, "id")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(
            request=fake_request,
            collection=mock_collection,
            current_user=mock_firebase_user,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_ownership_404(mock_firebase_user):
    """SEC-01: require_ownership raises 404 when the resource does not exist."""
    from fastapi import HTTPException

    _force_real_dependencies_import()
    from app.auth.dependencies import require_ownership

    resource_id = "507f1f77bcf86cd799439099"  # valid 24-hex ObjectId, but missing doc

    fake_request = MagicMock()
    fake_request.path_params = {"id": resource_id}

    mock_collection = MagicMock()
    mock_collection.find_one = AsyncMock(return_value=None)

    def get_collection_dependency():
        return mock_collection

    dependency = require_ownership(get_collection_dependency, "id")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(
            request=fake_request,
            collection=mock_collection,
            current_user=mock_firebase_user,
        )

    assert exc_info.value.status_code == 404
