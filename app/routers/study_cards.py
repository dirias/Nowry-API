from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from datetime import datetime, timedelta
from bson import ObjectId
from pymongo.collection import Collection
from app.models.StudyCard import StudyCard
from app.config.database import cards_collection
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/study-cards",
    tags=["study cards"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)

def get_cards_collection() -> Collection:
    return cards_collection


@router.post("/", summary="Create a new study card", response_model=StudyCard)
async def create_study_card(
    card: StudyCard,
    collection: Collection = Depends(get_cards_collection),
):
    logger.info(f"Creating study card: {card.title}")

    card.created_at = datetime.utcnow()
    card.ease_factor = 2.5
    card.interval = 1
    card.repetitions = 0

    card_dict = card.dict(by_alias=True, exclude={"id"})
    result = await collection.insert_one(card_dict)

    created_card = await collection.find_one({"_id": result.inserted_id})
    created_card["_id"] = str(created_card["_id"])
    return StudyCard(**created_card)


@router.get("/{id}", summary="Get a study card by ID", response_model=StudyCard)
async def get_study_card(
    id: str,
    collection: Collection = Depends(get_cards_collection),
):
    logger.info(f"Fetching study card with ID: {id}")

    card = await collection.find_one({"_id": ObjectId(id)})
    if not card:
        raise HTTPException(status_code=404, detail="Study card not found")

    card["_id"] = str(card["_id"])
    return StudyCard(**card)

@router.get("/", summary="List all study cards", response_model=List[StudyCard])
async def list_study_cards(
    limit: int = Query(10, ge=1, le=100),
    skip: int = Query(0, ge=0),
    tags: Optional[List[str]] = Query(None),
    search: Optional[str] = Query(None),
    collection: Collection = Depends(get_cards_collection),
):
    logger.info("Listing study cards...")

    query = {}
    if tags:
        query["tags"] = {"$in": tags}
    if search:
        query["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"content": {"$regex": search, "$options": "i"}},
        ]

    cursor = collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
    cards = await cursor.to_list(length=limit)

    for c in cards:
        c["_id"] = str(c["_id"])

    return [StudyCard(**c) for c in cards]


@router.patch("/{id}", summary="Update a study card", response_model=StudyCard)
async def update_study_card(
    id: str,
    updates: dict,
    collection: Collection = Depends(get_cards_collection),
):
    logger.info(f"Updating study card with ID: {id}")

    existing_card = await collection.find_one({"_id": ObjectId(id)})
    if not existing_card:
        raise HTTPException(status_code=404, detail="Study card not found")

    if "last_reviewed" in updates:
        updates["next_review"] = datetime.utcnow() + timedelta(
            days=existing_card.get("interval", 1)
        )

    result = await collection.update_one({"_id": ObjectId(id)}, {"$set": updates})
    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="No changes made")

    updated_card = await collection.find_one({"_id": ObjectId(id)})
    updated_card["_id"] = str(updated_card["_id"])
    return StudyCard(**updated_card)


@router.delete("/{id}", summary="Delete a study card", status_code=204)
async def delete_study_card(
    id: str,
    collection: Collection = Depends(get_cards_collection),
):
    logger.info(f"Deleting study card with ID: {id}")

    result = await collection.delete_one({"_id": ObjectId(id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Study card not found")

    return None
