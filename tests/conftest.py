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


@pytest.fixture
def mock_user_doc_plus():
    """User doc with Plus tier for testing Plus-gated endpoints."""
    from bson import ObjectId
    return {
        "_id": ObjectId("507f1f77bcf86cd799439011"),
        "user_id": "507f1f77bcf86cd799439011",
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
        "subscription": {"tier": "plus", "status": "active", "ai_usage_count": 0},
    }


@pytest.fixture
def mock_user_doc_pro():
    """User doc with Pro tier for testing Pro-gated endpoints."""
    from bson import ObjectId
    return {
        "_id": ObjectId("507f1f77bcf86cd799439011"),
        "user_id": "507f1f77bcf86cd799439011",
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
        "subscription": {"tier": "pro", "status": "active", "ai_usage_count": 0},
    }


@pytest.fixture
def mock_book_doc():
    """Minimal book document with Lexical JSON full_content."""
    from bson import ObjectId
    return {
        "_id": ObjectId("60b8d295f1d2c17f4e4b1234"),
        "user_id": "507f1f77bcf86cd799439011",
        "title": "Test Book",
        "full_content": '{"root":{"children":[{"type":"paragraph","children":[{"type":"text","text":"Hello world this is test content for AI expansion testing purposes."}]}]}}',
        "deleted_at": None,
    }


@pytest.fixture
def mock_books_collection(mock_book_doc):
    """Mock Motor books collection for AI magic tests."""
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=mock_book_doc)
    return collection


@pytest.fixture
def mock_deck_with_cards():
    """Mock deck document and associated cards for analyze-deck tests."""
    from bson import ObjectId
    deck_id = ObjectId("70b8d295f1d2c17f4e4b5678")
    return {
        "deck": {
            "_id": deck_id,
            "user_id": "507f1f77bcf86cd799439011",
            "name": "Test Deck",
            "deleted_at": None,
        },
        "cards": [
            {
                "_id": ObjectId(),
                "deck_id": deck_id,
                "user_id": "507f1f77bcf86cd799439011",
                "title": f"Card {i} front",
                "content": f"Card {i} back",
                "deleted_at": None,
            }
            for i in range(5)
        ],
    }
