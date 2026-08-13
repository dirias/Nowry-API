import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_unauthenticated_card_generation():
    """
    Ensure the /card/generate endpoint correctly throws a 401 Unauthorized
    when invoked without a valid Firebase Bearer token.
    """
    response = client.post("/card/generate", json={
        "prompt": "Make flashcards",
        "sampleText": "This should fail because no token was provided.",
        "sampleNumber": 3
    })
    
    assert response.status_code == 401, f"Expected 401, got {response.status_code}"
    assert response.json() == {"detail": "Not authenticated"}
