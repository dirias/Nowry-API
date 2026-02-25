from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.collection import Collection
from datetime import datetime
from typing import List, Optional
from app.models.Deck import Deck
from app.config.database import decks_collection
from app.utils.logger import get_logger
from app.auth.firebase_auth import get_firebase_user, optional_auth

router = APIRouter(
    prefix="/decks",
    tags=["decks"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_decks_collection() -> Collection:
    return decks_collection


@router.post(
    "",
    summary="Create a new deck",
    response_model=Deck,
    status_code=status.HTTP_201_CREATED,
)
async def create_deck(
    deck: Deck,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    logger.info(f"User {user.get('user_id')} is creating new deck: {deck.name}")

    deck.user_id = user.get("user_id")
    deck.created_at = datetime.utcnow()
    deck.updated_at = datetime.utcnow()

    deck_dict = deck.dict(by_alias=True, exclude={"id"})
    result = await collection.insert_one(deck_dict)

    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to insert deck")

    created_deck = await collection.find_one({"_id": result.inserted_id})

    # Convert ObjectIds to strings for response
    created_deck["_id"] = str(created_deck["_id"])
    if created_deck.get("user_id"):
        created_deck["user_id"] = str(created_deck["user_id"])
    if created_deck.get("cards"):
        created_deck["cards"] = [str(c) for c in created_deck["cards"]]

    return created_deck


@router.get("", summary="List all decks", response_model=List[Deck])
async def list_decks(
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    user_id = user.get("user_id")
    logger.info(f"Listing decks for user: {user_id}")

    # Filter by user_id to ensure users only see their own decks
    cursor = collection.find({"user_id": user_id, "deleted_at": None})
    decks = await cursor.to_list(length=100)

    for d in decks:
        d["_id"] = str(d["_id"])
        if d.get("user_id"):
            d["user_id"] = str(d["user_id"])
        if d.get("cards"):
            d["cards"] = [str(c) for c in d["cards"]]

    return decks


@router.get("/{id}", summary="Get deck by ID", response_model=Deck)
async def get_deck(
    id: str,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    from bson import ObjectId

    user_id = user.get("user_id")
    logger.info(f"User {user_id} fetching deck ID: {id}")

    try:
        deck = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        deck = await collection.find_one({"id": id})

    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    # Security check: ensure the deck belongs to the user
    if str(deck.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Not authorized to view this deck")

    deck["_id"] = str(deck["_id"])
    if deck.get("user_id"):
        deck["user_id"] = str(deck["user_id"])
    if deck.get("cards"):
        deck["cards"] = [str(c) for c in deck["cards"]]

    return deck


@router.get("/{id}/cards", summary="Get cards for a deck")
async def get_deck_cards(
    id: str,
    limit: int = 100,
    skip: int = 0,
    current_user: Optional[dict] = Depends(optional_auth),
):
    """Get all cards belonging to a specific deck (supports optional auth for public decks)"""
    from bson import ObjectId
    from app.config.database import cards_collection
    from app.auth.firebase_auth import optional_auth
    
    logger.info(f"Fetching cards for deck ID: {id}")
    
    # Verify deck exists
    try:
        deck = await decks_collection.find_one({"_id": ObjectId(id)})
    except Exception:
        deck = await decks_collection.find_one({"id": id})
    
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    
    # Security check: only allow if deck is public OR user owns it
    user_id = current_user.get("user_id") if current_user else None
    is_owner = user_id and str(deck.get("user_id")) == str(user_id)
    is_public = deck.get("is_public", False)  # Check root-level is_public
    
    if not is_owner and not is_public:
        raise HTTPException(status_code=403, detail="Not authorized to view this deck")
    
    # Fetch cards for this deck
    try:
        cards_cursor = cards_collection.find({
            "deck_id": ObjectId(id),
            "deleted_at": None
        }).skip(skip).limit(limit)
    except Exception:
        cards_cursor = cards_collection.find({
            "deck_id": id,
            "deleted_at": None
        }).skip(skip).limit(limit)
    
    cards = await cards_cursor.to_list(length=limit)
    
    # Convert ObjectIds to strings
    for card in cards:
        card["_id"] = str(card["_id"])
        if card.get("deck_id"):
            if isinstance(card["deck_id"], ObjectId):
                card["deck_id"] = str(card["deck_id"])
    
    return {"items": cards, "total": len(cards)}


@router.patch("/{id}", summary="Update a deck", response_model=Deck)
async def update_deck(
    id: str,
    updates: dict,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    from bson import ObjectId

    user_id = user.get("user_id")
    logger.info(f"User {user_id} updating deck ID: {id}")

    try:
        existing_deck = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        existing_deck = await collection.find_one({"id": id})

    if not existing_deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    if str(existing_deck.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to update this deck"
        )

    updates["updated_at"] = datetime.utcnow()

    # Do not allow updating internal fields
    for field in ["_id", "id", "user_id", "created_at"]:
        updates.pop(field, None)

    try:
        await collection.update_one({"_id": existing_deck["_id"]}, {"$set": updates})
    except Exception as e:
        logger.error(f"Error updating deck: {e}")
        raise HTTPException(status_code=400, detail="Update failed")

    updated_deck = await collection.find_one({"_id": existing_deck["_id"]})
    updated_deck["_id"] = str(updated_deck["_id"])
    if updated_deck.get("user_id"):
        updated_deck["user_id"] = str(updated_deck["user_id"])
    if updated_deck.get("cards"):
        updated_deck["cards"] = [str(c) for c in updated_deck["cards"]]

    return updated_deck


@router.delete("/{id}", summary="Delete a deck", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deck(
    id: str,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    from bson import ObjectId

    user_id = user.get("user_id")
    logger.info(f"User {user_id} deleting deck ID: {id}")

    try:
        existing_deck = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        existing_deck = await collection.find_one({"id": id})

    if not existing_deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    if str(existing_deck.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this deck"
        )

    now = datetime.utcnow()
    
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "is_public": False,  # Auto-unpublish
            "updated_at": now
        }
    }
    
    # 1. Soft delete the deck
    await collection.update_one(
        {"_id": existing_deck["_id"]},
        soft_delete_update
    )
    
    # 2. CASCADE: Soft delete all cards in this deck
    from app.config.database import cards_collection
    await cards_collection.update_many(
        {"deck_id": str(existing_deck["_id"]), "deleted_at": None},
        soft_delete_update
    )

    return None
