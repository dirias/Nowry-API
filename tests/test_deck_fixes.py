import sys
import os
import asyncio
import httpx
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId

# Add the app directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.auth.firebase_auth import get_firebase_user
from app.config.database import MONGO_URI, MONGO_DB

# --- Mocking ---

async def mock_user_a():
    return {"user_id": "507f1f77bcf86cd799439011", "email": "a@test.com"}

async def mock_user_b():
    return {"user_id": "507f191e810c19729de860ea", "email": "b@test.com"}

# --- Tests ---

async def run_tests():
    print("\n--- Starting Formal Deck Security Tests (Async) ---")
    
    # We use httpx.AsyncClient with ASGITransport for modern httpx compatibility
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        
        # 1. Setup - Create a deck as User A
        app.dependency_overrides[get_firebase_user] = mock_user_a
        
        deck_payload = {
            "name": "Security Test Deck",
            "description": "Testing BOLA and Deletion",
            "total_cards": 0
        }
        
        response = await client.post("/decks", json=deck_payload)
        assert response.status_code == 201
        deck_a = response.json()
        deck_a_id = deck_a["_id"]
        print(f"✅ Created Deck A ({deck_a_id}) as User A")

        # 2. BOLA Test - User B tries to access User A's deck
        app.dependency_overrides[get_firebase_user] = mock_user_b
        
        # Try to GET User A's deck
        response = await client.get(f"/decks/{deck_a_id}")
        assert response.status_code in [403, 404]
        print(f"✅ BOLA SUCCESS: User B blocked from GET-ing User A's deck (Got {response.status_code})")
        
        # Try to DELETE User A's deck
        response = await client.delete(f"/decks/{deck_a_id}")
        assert response.status_code in [403, 404]
        print(f"✅ BOLA SUCCESS: User B blocked from DELETE-ing User A's deck (Got {response.status_code})")

        # 3. Deletion Test - User A deletes their own deck
        app.dependency_overrides[get_firebase_user] = mock_user_a
        
        response = await client.delete(f"/decks/{deck_a_id}")
        assert response.status_code == 204
        print(f"✅ SUCCESS: User A successfully deleted their own deck")
        
        # 4. Verify filtered out - List decks for User A
        response = await client.get("/decks")
        assert response.status_code == 200
        decks = response.json()
        assert not any(d["_id"] == deck_a_id for d in decks)
        print(f"✅ SUCCESS: Deleted deck is hidden from list results")

    # 5. Cleanup Database (Sync)
    sync_client = MongoClient(MONGO_URI)
    sync_db = sync_client[MONGO_DB]
    sync_db.decks.delete_many({"user_id": {"$in": ["507f1f77bcf86cd799439011", "507f191e810c19729de860ea"]}})
    sync_client.close()
    print("✅ Cleanup complete (sync)")
    
    # Clear overrides
    app.dependency_overrides.clear()
    print("\n--- All Tests Passed Successfully ---")

if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    except Exception as e:
        print(f"\n❌ TEST FAILED: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
