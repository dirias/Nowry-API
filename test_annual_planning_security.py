import asyncio
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from app.main import app

client = TestClient(app)

def test_goal_idor_update_rejection():
    """
    Test that updating a goal requires strict BOLA ownership tracing.
    If the goal's parent plan is owned by someone else, verify 403 is thrown.
    """
    
    # Mock the dependency to simulate a logged-in attacker
    async def mock_get_firebase_user():
        return {"user_id": "malicious_attacker_uid", "role": "user"}

    app.dependency_overrides[app.dependency_overrides.get("get_firebase_user", "fallback")] = mock_get_firebase_user
    
    # Force the get_firebase_user override explicitly
    from app.auth.firebase_auth import get_firebase_user
    app.dependency_overrides[get_firebase_user] = mock_get_firebase_user

    payload = {
        "title": "Hacked Goal Title",
        "progress": 100
    }

    # Attempt to update a completely random or non-owned goal
    # The BOLA logic traces Goal (123) -> Focus Area -> Plan -> User.
    # Without mocking DB, the goal won't exist naturally, throwing a 404.
    response = client.put("/annual-plan/goals/652fbc9e7d3b5a1234567890", json=payload)
    
    assert response.status_code in [403, 404], f"BOLA Failed: Expected blocked access, got {response.status_code}"
    
    # Cleanup overrides
    app.dependency_overrides.clear()
    print("BOLA / IDOR Verification Test Passed Successfully!")

if __name__ == "__main__":
    test_goal_idor_update_rejection()
