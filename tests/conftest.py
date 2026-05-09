import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


@pytest.fixture
def mock_firebase_user():
    """Simulates get_firebase_user dependency output."""
    return {
        "user_id": "507f1f77bcf86cd799439011",
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
    }


@pytest.fixture
def mock_user_doc():
    """Simulates a MongoDB user document with subscription fields."""
    from bson import ObjectId
    return {
        "_id": ObjectId("507f1f77bcf86cd799439011"),
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
        "stripe_customer_id": "cus_test123",
        "subscription": {
            "tier": "free",
            "status": "active",
            "ai_usage_count": 0,
            "ai_usage_reset_date": datetime.now(timezone.utc),
            "next_billing_date": None,
            "stripe_subscription_id": None,
            "billing_interval": None,
            "subscription_status_updated_at": datetime.now(timezone.utc),
        },
    }


@pytest.fixture
def mock_users_collection(mock_user_doc):
    """Mock Motor collection with common async methods."""
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=mock_user_doc)
    collection.find_one_and_update = AsyncMock(return_value=mock_user_doc)
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id="evt_123"))
    return collection


@pytest.fixture
def mock_stripe_processed_events_collection():
    """Mock collection for stripe processed events deduplication."""
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=None)  # not yet processed
    collection.insert_one = AsyncMock(return_value=MagicMock())
    return collection
