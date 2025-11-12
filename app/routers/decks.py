from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.collection import Collection
from datetime import datetime
from uuid import uuid4
from app.config.database import decks_collection
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/decks",
    tags=["decks"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)

def get_decks_collection() -> Collection:
    return decks_collection


@router.post("/", summary="Create a new deck", status_code=status.HTTP_201_CREATED)
async def create_deck(deck: dict, collection: Collection = Depends(get_decks_collection)):

    logger.info(f"Creating new deck: {deck.get('name')}")


    if "name" not in deck or not deck["name"].strip():
        raise HTTPException(status_code=400, detail="Deck name is required")

    new_deck = {
        "id": str(uuid4()),
        "name": deck["name"],
        "description": deck.get("description", ""),
        "cards": deck.get("cards", []),
        "user_id": deck.get("user_id"),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "deleted_at": None
    }

    result = await collection.insert_one(new_deck)

    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to insert deck")

    logger.info(f"Deck created successfully with ID: {new_deck['id']}")
    return new_deck
