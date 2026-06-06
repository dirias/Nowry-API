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


@pytest.fixture
def mock_book_doc_with_counter():
    """Book document with illustration_count field for Phase 6 tests."""
    from bson import ObjectId
    return {
        "_id": ObjectId("60b8d295f1d2c17f4e4b1234"),
        "user_id": "507f1f77bcf86cd799439011",
        "title": "Test Book",
        "full_content": '{"root":{"children":[]}}',
        "deleted_at": None,
        "illustration_count": 0,
    }


@pytest.fixture
def mock_tts_client():
    """Mock google.cloud.texttospeech.TextToSpeechClient for TTS tests."""
    from unittest.mock import MagicMock
    client = MagicMock()
    mock_response = MagicMock()
    mock_response.audio_content = b"fake-mp3-bytes"
    client.synthesize_speech.return_value = mock_response
    return client


# --------------------------------------------------------------------------- #
# Phase 7 — Blackboard & Smart Pet fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_blackboard_doc():
    """Simulates a MongoDB blackboard document with Phase 7 multi-board fields."""
    from bson import ObjectId
    return {
        "_id": ObjectId("617f1f77bcf86cd799439abc"),
        "board_id": "main",
        "owner_user_id": "507f1f77bcf86cd799439011",
        "collaborators": [],
        "name": "My Blackboard",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


@pytest.fixture
def mock_invitation():
    """Simulates a share invitation payload."""
    return {"invitee_email": "collaborator@example.com"}


@pytest.fixture
def mock_blackboards_collection(mock_blackboard_doc):
    """Mock Motor collection for blackboards."""
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=mock_blackboard_doc)
    collection.find_one_and_update = AsyncMock(return_value=mock_blackboard_doc)
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id="617f1f77bcf86cd799439abc"))
    collection.count_documents = AsyncMock(return_value=0)
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[mock_blackboard_doc])
    collection.find = MagicMock(return_value=mock_cursor)
    return collection
