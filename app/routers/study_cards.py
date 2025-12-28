from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import List, Optional
from datetime import datetime, timedelta
from bson import ObjectId
from pymongo.collection import Collection
from app.models.StudyCard import StudyCard
from app.config.database import cards_collection, decks_collection
from app.utils.logger import get_logger
from app.auth.auth import get_current_user_authorization

router = APIRouter(
    prefix="/study-cards",
    tags=["study cards"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_cards_collection() -> Collection:
    return cards_collection


def get_decks_collection() -> Collection:
    return decks_collection


@router.post("/", summary="Create a new study card", response_model=StudyCard)
async def create_study_card(
    card: StudyCard,
    collection: Collection = Depends(get_cards_collection),
    d_collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_current_user_authorization),
):
    user_id = user.get("user_id")
    logger.info(f"User {user_id} creating study card: {card.title}")

    card.user_id = user_id
    card.created_at = datetime.utcnow()
    card.ease_factor = 2.5
    card.interval = 1
    card.repetitions = 0

    card_dict = card.dict(by_alias=True, exclude={"id"})
    result = await collection.insert_one(card_dict)
    card_id = result.inserted_id

    # Sync with Deck if deck_id is provided
    if card.deck_id:
        await d_collection.update_one(
            {"_id": ObjectId(card.deck_id)},
            {"$inc": {"total_cards": 1}, "$push": {"cards": card_id}},
        )

    created_card = await collection.find_one({"_id": card_id})
    created_card["_id"] = str(created_card["_id"])
    if created_card.get("deck_id"):
        created_card["deck_id"] = str(created_card["deck_id"])
    if created_card.get("user_id"):
        created_card["user_id"] = str(created_card["user_id"])

    return created_card


# Statistics endpoint for dashboard (MUST be before /{id} route)
@router.get("/statistics", summary="Get study statistics for the current user")
async def get_statistics(
    collection: Collection = Depends(get_cards_collection),
    current_user: dict = Depends(get_current_user_authorization),
):
    """
    Get study statistics including:
    - Weekly progress (cards reviewed per day)
    - Recent performance (last reviews with scores)
    """
    try:
        user_id = current_user.get("user_id")
        logger.info(f"Fetching statistics for user {user_id}")

        # Get all cards for the user
        all_cards = await collection.find({"user_id": user_id}).to_list(None)
        
        # Get books collection for book stats
        from app.config.database import books_collection
        all_books = await books_collection.find({"user_id": user_id}).to_list(None)

        # Calculate weekly progress (last 7 days) - separated by type
        from datetime import datetime, timedelta

        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_data = []

        for i in range(6, -1, -1):  # Last 7 days (6 days ago to today)
            day_start = today - timedelta(days=i)
            day_end = day_start + timedelta(days=1)

            # Count by card type
            flashcards_count = sum(
                1
                for card in all_cards
                if card.get("last_reviewed")
                and day_start <= card["last_reviewed"] < day_end
                and card.get("card_type") in [None, "flashcard", "studycard"]
            )
            
            quizzes_count = sum(
                1
                for card in all_cards
                if card.get("last_reviewed")
                and day_start <= card["last_reviewed"] < day_end
                and card.get("card_type") == "quiz"
            )
            
            visual_count = sum(
                1
                for card in all_cards
                if card.get("last_reviewed")
                and day_start <= card["last_reviewed"] < day_end
                and card.get("card_type") == "visual"
            )
            
            # Count books accessed/updated on this day
            books_count = sum(
                1
                for book in all_books
                if book.get("updated_at")
                and day_start <= book["updated_at"] < day_end
            )
            
            total_count = flashcards_count + quizzes_count + visual_count + books_count

            weekly_data.append(
                {
                    "day": day_start.strftime("%A")[:3],  # Mon, Tue, etc.
                    "date": day_start.strftime("%Y-%m-%d"),
                    "cards": total_count,  # Keep for backwards compatibility
                    "flashcards": flashcards_count,
                    "quizzes": quizzes_count,
                    "visual": visual_count,
                    "books": books_count,
                }
            )

        # Get recent performance (last 10 reviews) - include type
        reviewed_cards = [card for card in all_cards if card.get("last_reviewed")]
        reviewed_cards.sort(
            key=lambda x: x.get("last_reviewed", datetime.min), reverse=True
        )

        recent_performance = []
        for card in reviewed_cards[:10]:
            # Calculate performance score based on ease_factor
            ease = card.get("ease_factor", 2.5)
            score = min(10, max(1, int((ease - 1.3) / (2.5 - 1.3) * 10)))
            
            # Determine card type
            card_type = card.get("card_type")
            if card_type in [None, "studycard"]:
                card_type = "flashcard"

            recent_performance.append(
                {
                    "date": (
                        card.get("last_reviewed").strftime("%A, %d %b")
                        if card.get("last_reviewed")
                        else "Unknown"
                    ),
                    "card_title": card.get("title", "Untitled"),
                    "score": score,  # Just the number, not formatted
                    "type": card_type,
                    "ease_factor": ease,
                }
            )
        
        # Add recent book activity
        recent_books = [book for book in all_books if book.get("updated_at")]
        recent_books.sort(
            key=lambda x: x.get("updated_at", datetime.min), reverse=True
        )
        
        for book in recent_books[:3]:  # Add top 3 recent books
            recent_performance.insert(0, {
                "date": (
                    book.get("updated_at").strftime("%A, %d %b")
                    if book.get("updated_at")
                    else "Unknown"
                ),
                "card_title": book.get("title", "Untitled Book"),
                "score": 10,  # Books don't have scores, default to 10
                "type": "book",
            })
        
        # Keep only last 10 total
        recent_performance = recent_performance[:10]

        # Overall stats
        total_cards = len(all_cards)
        reviewed_count = len(reviewed_cards)
        new_cards = total_cards - reviewed_count

        # Current streak (days with at least 1 review)
        streak = 0
        check_date = today
        while True:
            day_start = check_date
            day_end = check_date + timedelta(days=1)
            reviewed_today = any(
                card.get("last_reviewed")
                and day_start <= card["last_reviewed"] < day_end
                for card in all_cards
            )
            if reviewed_today:
                streak += 1
                check_date -= timedelta(days=1)
            else:
                break

        return {
            "weekly_progress": weekly_data,
            "recent_performance": recent_performance,
            "summary": {
                "total_cards": total_cards,
                "reviewed_cards": reviewed_count,
                "new_cards": new_cards,
                "current_streak": streak,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching statistics: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error fetching statistics: {str(e)}"
        )


@router.get("/{id}", summary="Get a study card by ID", response_model=StudyCard)
async def get_study_card(
    id: str,
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_current_user_authorization),
):
    user_id = user.get("user_id")
    logger.info(f"User {user_id} fetching study card with ID: {id}")

    try:
        card = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid card ID")

    if not card:
        raise HTTPException(status_code=404, detail="Study card not found")

    if str(card.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Not authorized to view this card")

    card["_id"] = str(card["_id"])
    if card.get("deck_id"):
        card["deck_id"] = str(card["deck_id"])
    if card.get("user_id"):
        card["user_id"] = str(card["user_id"])

    return card


@router.get("/", summary="List all study cards", response_model=List[StudyCard])
async def list_study_cards(
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0),
    tags: Optional[List[str]] = Query(None),
    search: Optional[str] = Query(None),
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_current_user_authorization),
):
    user_id = user.get("user_id")
    logger.info(f"Listing study cards for user: {user_id}")

    query = {"user_id": user_id}
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
        if c.get("deck_id"):
            c["deck_id"] = str(c.get("deck_id"))
        if c.get("user_id"):
            c["user_id"] = str(c.get("user_id"))

    return cards


@router.patch("/{id}", summary="Update a study card", response_model=StudyCard)
async def update_study_card(
    id: str,
    updates: dict,
    collection: Collection = Depends(get_cards_collection),
    d_collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_current_user_authorization),
):
    user_id = user.get("user_id")
    logger.info(f"User {user_id} updating study card with ID: {id}")

    try:
        existing_card = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid card ID")

    if not existing_card:
        raise HTTPException(status_code=404, detail="Study card not found")

    if str(existing_card.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to update this card"
        )

    # Handle deck_id change
    new_deck_id = updates.get("deck_id")
    old_deck_id = existing_card.get("deck_id")

    if "deck_id" in updates and str(new_deck_id) != str(old_deck_id):
        # Remove from old deck
        if old_deck_id:
            await d_collection.update_one(
                {"_id": ObjectId(old_deck_id)},
                {"$inc": {"total_cards": -1}, "$pull": {"cards": ObjectId(id)}},
            )
        # Add to new deck
        if new_deck_id:
            updates["deck_id"] = ObjectId(new_deck_id)
            await d_collection.update_one(
                {"_id": ObjectId(new_deck_id)},
                {"$inc": {"total_cards": 1}, "$push": {"cards": ObjectId(id)}},
            )
        else:
            updates["deck_id"] = None

    # Prevent internal field modification
    for field in ["_id", "id", "user_id", "created_at"]:
        updates.pop(field, None)

    if "last_reviewed" in updates:
        updates["next_review"] = datetime.utcnow() + timedelta(
            days=existing_card.get("interval", 1)
        )

    await collection.update_one({"_id": ObjectId(id)}, {"$set": updates})

    updated_card = await collection.find_one({"_id": ObjectId(id)})
    updated_card["_id"] = str(updated_card["_id"])
    if updated_card.get("deck_id"):
        updated_card["deck_id"] = str(updated_card["deck_id"])
    if updated_card.get("user_id"):
        updated_card["user_id"] = str(updated_card["user_id"])

    return updated_card


@router.delete(
    "/{id}", summary="Delete a study card", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_study_card(
    id: str,
    collection: Collection = Depends(get_cards_collection),
    d_collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_current_user_authorization),
):
    user_id = user.get("user_id")
    logger.info(f"User {user_id} deleting study card with ID: {id}")

    try:
        existing_card = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid card ID")

    if not existing_card:
        raise HTTPException(status_code=404, detail="Study card not found")

    if str(existing_card.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this card"
        )

    # Sync with Deck if needed
    deck_id = existing_card.get("deck_id")
    if deck_id:
        await d_collection.update_one(
            {"_id": ObjectId(deck_id)},
            {"$inc": {"total_cards": -1}, "$pull": {"cards": ObjectId(id)}},
        )

    await collection.delete_one({"_id": ObjectId(id)})
    return None


# Review endpoint for SM-2 algorithm
@router.post("/{id}/review", summary="Review a card with SM-2 grading")
async def review_card(
    id: str,
    grade: str = Query(..., pattern="^(again|hard|good|easy)$"),
    collection: Collection = Depends(get_cards_collection),
    current_user: dict = Depends(get_current_user_authorization),
):
    """
    Review a card and update its SM-2 spaced repetition parameters.

    - **grade**: User's self-assessment (again, hard, good, easy)
    """
    try:
        from app.utils.sm2 import calculate_next_review

        user_id = current_user.get("user_id")
        logger.info(f"Reviewing card {id} with grade {grade} for user {user_id}")

        # Fetch the card
        card = await collection.find_one({"_id": ObjectId(id)})
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")

        # Authorization check
        if str(card.get("user_id")) != str(user_id):
            raise HTTPException(
                status_code=403, detail="Not authorized to review this card"
            )

        # Get current SM-2 parameters
        ease_factor = card.get("ease_factor", 2.5)
        interval = card.get("interval", 1)
        repetitions = card.get("repetitions", 0)

        logger.info(
            f"Current SM-2: ease={ease_factor}, interval={interval}, reps={repetitions}"
        )

        # Calculate new parameters using SM-2
        sm2_result = calculate_next_review(
            grade=grade,
            ease_factor=ease_factor,
            interval=interval,
            repetitions=repetitions,
        )

        logger.info(f"New SM-2: {sm2_result}")

        # Update the card
        await collection.update_one(
            {"_id": ObjectId(id)},
            {
                "$set": {
                    "last_reviewed": sm2_result["last_reviewed"],
                    "next_review": sm2_result["next_review"],
                    "ease_factor": sm2_result["ease_factor"],
                    "interval": sm2_result["interval"],
                    "repetitions": sm2_result["repetitions"],
                }
            },
        )

        logger.info(f"Successfully updated card {id}")

        return {"message": "Card reviewed successfully", "sm2_data": sm2_result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reviewing card: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reviewing card: {str(e)}")
