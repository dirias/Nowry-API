from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from pymongo.collection import Collection
from app.models.StudyCard import StudyCard
from app.models.deck_config import resolve_deck_budget
from app.config.database import cards_collection, decks_collection
from app.utils.logger import get_logger
from app.auth.firebase_auth import get_firebase_user

from app.auth.dependencies import require_ownership

router = APIRouter(
    prefix="/study-cards",
    tags=["study cards"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_cards_collection() -> Collection:
    return cards_collection


def get_decks_collection() -> Collection:
    return decks_collection


async def _verify_deck_ownership(deck_id: str, user_id: str):
    """Verify that a deck exists and belongs to the user."""
    if not deck_id:
        return
    try:
        obj_id = ObjectId(deck_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deck ID format")

    deck = await decks_collection.find_one({"_id": obj_id, "deleted_at": None})
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    if str(deck.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to access this deck"
        )


async def _select_session_cards(
    collection: Collection,
    user_id: str,
    deck_or: list,
    new_cap: int,
    review_cap: int,
    now_dt: datetime,
    today_start: datetime,
) -> tuple[list, list]:
    """Pick the new + review cards for a study session, locking today's plan.

    New-card selection is sticky for the day:
      1. Cards already introduced today (`introduced_at >= today_start`) are the
         day's pool. Cards in that pool that have not been graded yet
         (`last_reviewed is None`) are still pending.
      2. If the day's pool is smaller than `new_cap`, top it up with fresh
         never-seen cards (`introduced_at` unset, `last_reviewed` None) and
         stamp them with `introduced_at = now` so they persist across sessions.
      3. The session returns all pending cards from the locked pool.

    Review cards keep the simple "due now, capped by remaining daily budget"
    behaviour — they are intrinsically deterministic by `next_review`.
    """
    base_match = {"user_id": user_id, "deleted_at": None, "$or": deck_or}

    # 1. Calculate how many NEW cards were studied today to adjust the remaining budget
    new_studied_today = await collection.count_documents({
        **base_match,
        "last_reviewed": {"$gte": today_start},
        "repetitions": {"$lte": 1},
    })
    new_remaining = max(0, new_cap - new_studied_today)

    # 2. Get the currently active "sticky pool" for today
    # We fetch cards introduced today that HAVEN'T been reviewed yet.
    # We fetch up to `new_remaining` to fill our budget.
    todays_pending_pool = await collection.find({
        **base_match,
        "introduced_at": {"$gte": today_start},
        "last_reviewed": None,
    }).sort("introduced_at", 1).to_list(length=new_remaining)

    # 3. If we still have slots to fill after checking the sticky pool, top it up
    slots_to_fill = max(0, new_remaining - len(todays_pending_pool))
    new_cards_raw = list(todays_pending_pool)
    if slots_to_fill > 0:
        # A card is eligible to be (re)introduced today if it has never been
        # graded (last_reviewed is None). This covers both brand-new cards and
        # cards that were stamped on a previous day but never actually studied.
        fresh_query = {
            "user_id": user_id,
            "deleted_at": None,
            "last_reviewed": None,
            "$and": [
                {"$or": deck_or},
                {"$or": [
                    {"introduced_at": None},
                    {"introduced_at": {"$exists": False}},
                    {"introduced_at": {"$lt": today_start}},
                ]},
            ],
        }
        fresh_cards = await collection.find(fresh_query).sort("created_at", 1).limit(slots_to_fill).to_list(length=slots_to_fill)
        if fresh_cards:
            fresh_ids = [c["_id"] for c in fresh_cards]
            await collection.update_many(
                {"_id": {"$in": fresh_ids}},
                {"$set": {"introduced_at": now_dt}},
            )
            for c in fresh_cards:
                c["introduced_at"] = now_dt
            new_cards_raw.extend(fresh_cards)

    # We already have new_cards_raw from steps above

    # --- Review cards: due now, capped by remaining daily review budget ---
    reviews_done_today = await collection.count_documents({
        **base_match,
        "last_reviewed": {"$gte": today_start},
        "repetitions": {"$gt": 1},
    })
    review_remaining = max(0, review_cap - reviews_done_today)

    review_cards_raw: list = []
    if review_remaining > 0:
        review_query = {
            **base_match,
            "last_reviewed": {"$ne": None},
            "next_review": {"$lte": now_dt},
        }
        review_cards_raw = await collection.find(review_query).sort("next_review", 1).limit(review_remaining).to_list(length=review_remaining)

    return new_cards_raw, review_cards_raw


def _get_deck_budget(deck_doc: dict) -> tuple[int, int]:
    """Return (new_per_day, max_reviews_per_day) from a deck document.

    Thin wrapper around the shared `resolve_deck_budget` so this router cannot
    drift from the dashboard counts in routers/decks.py.
    """
    _, new_per_day, max_reviews = resolve_deck_budget(deck_doc)
    return new_per_day, max_reviews


@router.post("", summary="Create a new study card", response_model=StudyCard)
async def create_study_card(
    card: StudyCard,
    collection: Collection = Depends(get_cards_collection),
    d_collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    user_id = user.get("user_id")

    # --- Subscription Limit Check ---
    from app.config.database import users_collection
    from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier

    # Get user subscription
    user_data = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    subscription_data = user_data.get("subscription", {"tier": "free"})
    tier_key = subscription_data.get("tier", "free")
    plan = SUBSCRIPTION_PLANS.get(tier_key, SUBSCRIPTION_PLANS[SubscriptionTier.FREE])

    # Determine card type and applicable limit
    limit_key = "flashcards"  # default
    query_type = {"$in": [None, "basic", "flashcard", "studycard"]}

    if card.card_type == "quiz":
        limit_key = "quiz_questions"
        query_type = "quiz"
    elif card.card_type == "visual":
        limit_key = "visual_diagrams"
        query_type = "visual"

    limit_value = plan["limits"].get(limit_key, 0)

    # Check limit if not unlimited (-1)
    if limit_value != -1:
        # Count only cards of this type
        current_count = await collection.count_documents(
            {"user_id": user_id, "card_type": query_type}
        )

        if current_count >= limit_value:
            # Format readable name
            feature_name = limit_key.replace("_", " ").title()
            raise HTTPException(
                status_code=403,
                detail=f"{feature_name} limit reached for {plan['name']} plan. Upgrade to create more.",
            )
    # --------------------------------

    logger.info(f"User {user_id} creating study card: {card.title}")

    card.user_id = user_id
    card.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    card.ease_factor = 2.5
    card.interval = 1
    card.repetitions = 0

    card_dict = card.model_dump(by_alias=True, exclude={"id"})
    result = await collection.insert_one(card_dict)
    card_id = result.inserted_id

    # Sync with Deck if deck_id is provided
    if card.deck_id:
        await _verify_deck_ownership(card.deck_id, user_id)
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
    current_user: dict = Depends(get_firebase_user),
):
    """
    Get study statistics including:
    - Weekly progress (cards reviewed per day)
    - Recent performance (last reviews with scores)
    """
    try:
        user_id = current_user.get("user_id")
        logger.info(f"Fetching statistics for user {user_id}")

        # Only fetch reviewed cards needed for streak, weekly progress, and recent performance.
        # Bounded to last 90 days to avoid loading the full card corpus into memory.
        from datetime import datetime, timedelta, timezone

        ninety_days_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
        all_cards = await collection.find(
            {
                "user_id": user_id,
                "deleted_at": None,
                "last_reviewed": {"$gte": ninety_days_ago},
            }
        ).to_list(length=2000)

        # Get books collection for book stats
        from app.config.database import books_collection

        all_books = await books_collection.find({"user_id": user_id}).to_list(length=500)

        # Calculate weekly progress (last 7 days) - separated by type
        today = datetime.now(timezone.utc).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
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
                and card.get("card_type") in [None, "flashcard"]
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
                if book.get("updated_at") and day_start <= book["updated_at"] < day_end
            )

            total_count = flashcards_count + quizzes_count + visual_count

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

            # Determine card type (default to flashcard if not specified)
            card_type = card.get("card_type") or "flashcard"

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
        recent_books.sort(key=lambda x: x.get("updated_at", datetime.min), reverse=True)

        for book in recent_books[:3]:  # Add top 3 recent books
            recent_performance.insert(
                0,
                {
                    "date": (
                        book.get("updated_at").strftime("%A, %d %b")
                        if book.get("updated_at")
                        else "Unknown"
                    ),
                    "card_title": book.get("title", "Untitled Book"),
                    "score": 10,  # Books don't have scores, default to 10
                    "type": "book",
                },
            )

        # Keep only last 10 total
        recent_performance = recent_performance[:10]

        # Overall stats — use count_documents so deleted cards are excluded and
        # the counts are not skewed by the 90-day window on all_cards.
        total_cards = await collection.count_documents(
            {"user_id": user_id, "deleted_at": None}
        )
        reviewed_count = await collection.count_documents(
            {"user_id": user_id, "deleted_at": None, "last_reviewed": {"$ne": None}}
        )
        new_cards = total_cards - reviewed_count

        # Get GLOBAL due cards count (accurate across all cards)
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        due_today_count = await collection.count_documents({
            "user_id": user_id,
            "deleted_at": None,
            "$or": [
                {"next_review": {"$exists": False}},
                {"next_review": None},
                {"next_review": {"$lte": now_dt}}
            ]
        })

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

        # last_session_struggle: front of the card most recently reviewed
        # yesterday or today AND with repetitions <= 1 (wrong answer resets
        # repetitions to 1 in SM-2).  Truncated to 60 chars.
        yesterday_start = today - timedelta(days=1)
        struggle_cards = await collection.find(
            {
                "user_id": user_id,
                "deleted_at": None,
                "last_reviewed": {"$gte": yesterday_start},
                "repetitions": {"$lte": 1},
            }
        ).sort("last_reviewed", -1).to_list(length=50)

        last_session_struggle: Optional[str] = None
        if struggle_cards:
            front: str = (struggle_cards[0].get("front") or "").strip()
            if front:
                last_session_struggle = front[:60] if len(front) <= 60 else front[:60]

        return {
            "weekly_progress": weekly_data,
            "recent_performance": recent_performance,
            "summary": {
                "total_cards": total_cards,
                "reviewed_cards": reviewed_count,
                "new_cards": new_cards,
                "due_today": due_today_count,
                "current_streak": streak,
                "last_session_struggle": last_session_struggle,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching statistics: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error fetching statistics: {str(e)}"
        )


@router.get("/tags", summary="Get all tags used by the current user's cards")
async def get_card_tags(
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
):
    user_id = user.get("user_id")

    # Mirror the same active-deck filter used in list_study_cards so tag counts
    # match the number of cards actually returned when a tag is selected.
    active_decks = await decks_collection.find(
        {"user_id": user_id, "deleted_at": None}, {"_id": 1}
    ).to_list(length=500)
    active_deck_ids = []
    for d in active_decks:
        active_deck_ids.append(d["_id"])
        active_deck_ids.append(str(d["_id"]))

    pipeline = [
        {"$match": {
            "user_id": user_id,
            "deleted_at": None,
            "tags": {"$exists": True, "$ne": []},
            "$or": [
                {"deck_id": None},
                {"deck_id": {"$exists": False}},
                {"deck_id": {"$in": active_deck_ids}},
            ],
        }},
        {"$unwind": "$tags"},
        {"$group": {"_id": "$tags", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$project": {"_id": 0, "tag": "$_id", "count": 1}}
    ]
    tags = await collection.aggregate(pipeline).to_list(length=500)
    return tags


@router.get("/daily-review", summary="Get today's locked daily review session across all decks")
async def get_daily_review_cards(
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
) -> dict:
    """Aggregate today's session across every active deck.

    Each deck contributes its own locked new-card pool (capped at the deck's
    `new_per_day`) and its own remaining review budget. The selection is sticky
    for the day via `introduced_at` — the same cards reappear across sessions
    until they are graded.
    """
    user_id = user.get("user_id")
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    active_decks = await decks_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=500)

    all_new: list = []
    all_review: list = []

    for deck in active_decks:
        new_cap, review_cap = _get_deck_budget(deck)
        deck_oid = deck["_id"]
        deck_or = [{"deck_id": deck_oid}, {"deck_id": str(deck_oid)}]

        new_raw, review_raw = await _select_session_cards(
            collection=collection,
            user_id=user_id,
            deck_or=deck_or,
            new_cap=new_cap,
            review_cap=review_cap,
            now_dt=now_dt,
            today_start=today_start,
        )
        all_new.extend(new_raw)
        all_review.extend(review_raw)

    cards = all_new + all_review
    for c in cards:
        c["_id"] = str(c["_id"])
        if c.get("deck_id"):
            c["deck_id"] = str(c["deck_id"])
        if c.get("user_id"):
            c["user_id"] = str(c["user_id"])

    return {
        "cards": cards,
        "total": len(cards),
        "page": 1,
        "has_more": False,
    }


@router.get("/{id}", summary="Get a study card by ID", response_model=StudyCard)
async def get_study_card(
    card: dict = Depends(require_ownership(get_cards_collection, "id")),
):
    return card


@router.get("", summary="List all study cards")
async def list_study_cards(
    limit: int = Query(50, ge=1, le=500),
    skip: int = Query(0, ge=0),
    tags: Optional[List[str]] = Query(None),
    search: Optional[str] = Query(None),
    deck_id: Optional[str] = Query(None),
    due_only: bool = Query(False),
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
) -> dict:
    user_id = user.get("user_id")
    logger.info(f"Listing study cards for user: {user_id}")

    query: dict = {
        "user_id": user_id,
        "deleted_at": None,
    }

    if deck_id is not None:
        # Direct deck filter — skip the active-decks query entirely
        try:
            deck_oid = ObjectId(deck_id)
            deck_filter: list = [deck_oid, deck_id]
        except Exception:
            deck_filter = [deck_id]
        query["$or"] = [{"deck_id": v} for v in deck_filter]
    else:
        # Collect active deck IDs (both ObjectId and string forms) to exclude orphans
        active_decks = await decks_collection.find(
            {"user_id": user_id, "deleted_at": None}, {"_id": 1}
        ).to_list(length=500)
        active_deck_ids: list = []
        for d in active_decks:
            active_deck_ids.append(d["_id"])
            active_deck_ids.append(str(d["_id"]))
        query["$or"] = [
            {"deck_id": None},
            {"deck_id": {"$exists": False}},
            {"deck_id": {"$in": active_deck_ids}},
        ]

    if tags:
        query["tags"] = {"$in": tags}

    due_clause: Optional[dict] = None
    if due_only:
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        due_clause = {"$or": [
            {"next_review": {"$exists": False}},
            {"next_review": None},
            {"next_review": {"$lte": now_dt}},
        ]}

    if search:
        import re
        safe_search = re.escape(search)
        search_or = {"$or": [
            {"title": {"$regex": safe_search, "$options": "i"}},
            {"content": {"$regex": safe_search, "$options": "i"}},
        ]}
        and_clauses: list = [{"$or": query.pop("$or")}, search_or]
        if due_clause is not None:
            and_clauses.append(due_clause)
        query["$and"] = and_clauses
    elif due_clause is not None:
        # Wrap existing top-level $or and the due clause together
        query["$and"] = [{"$or": query.pop("$or")}, due_clause]

    # When fetching due cards for a specific deck, apply the deck's daily budget:
    # new cards (never reviewed) are capped at new_per_day,
    # review cards (reviewed before, now past due) are capped at max_reviews_per_day.
    if deck_id is not None and due_only:
        deck_doc = await decks_collection.find_one(
            {"_id": ObjectId(deck_id) if (deck_id and ObjectId.is_valid(deck_id)) else None, "deleted_at": None}
        )
        new_cap, review_cap = _get_deck_budget(deck_doc) if deck_doc else (20, 100)
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        deck_obj_id = ObjectId(deck_id) if (deck_id and ObjectId.is_valid(deck_id)) else None
        deck_or = [{"deck_id": deck_obj_id}]
        if deck_id:
            deck_or.append({"deck_id": deck_id})
        new_cards_raw, review_cards_raw = await _select_session_cards(
            collection=collection,
            user_id=user_id,
            deck_or=deck_or,
            new_cap=new_cap,
            review_cap=review_cap,
            now_dt=now_dt,
            today_start=today_start,
        )

        cards = new_cards_raw + review_cards_raw
        for c in cards:
            c["_id"] = str(c["_id"])
            if c.get("deck_id"):
                c["deck_id"] = str(c["deck_id"])
            if c.get("user_id"):
                c["user_id"] = str(c["user_id"])

        return {
            "cards": cards,
            "total": len(cards),
            "page": 1,
            "has_more": False,
        }

    # Generic path — paginated, no daily budget applied
    total = await collection.count_documents(query)

    cursor = collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
    cards = await cursor.to_list(length=limit)

    for c in cards:
        c["_id"] = str(c["_id"])
        if c.get("deck_id"):
            c["deck_id"] = str(c.get("deck_id"))
        if c.get("user_id"):
            c["user_id"] = str(c.get("user_id"))

    return {
        "cards": cards,
        "total": total,
        "page": (skip // limit) + 1 if limit > 0 else 1,
        "has_more": skip + len(cards) < total,
    }


@router.patch("/{id}", summary="Update a study card", response_model=StudyCard)
async def update_study_card(
    id: str,
    updates: dict,
    collection: Collection = Depends(get_cards_collection),
    d_collection: Collection = Depends(get_decks_collection),
    existing_card: dict = Depends(require_ownership(get_cards_collection, "id")),
):

    # Handle deck_id change
    new_deck_id = updates.get("deck_id")
    old_deck_id = existing_card.get("deck_id")

    if "deck_id" in updates and str(new_deck_id) != str(old_deck_id):
        # Remove from old deck
        if old_deck_id:
            await _verify_deck_ownership(old_deck_id, existing_card.get("user_id"))
            await d_collection.update_one(
                {"_id": ObjectId(old_deck_id)},
                {"$inc": {"total_cards": -1}, "$pull": {"cards": ObjectId(id)}},
            )
        # Add to new deck
        if new_deck_id:
            await _verify_deck_ownership(new_deck_id, existing_card.get("user_id"))
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
        updates["next_review"] = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
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
    existing_card: dict = Depends(require_ownership(get_cards_collection, "id")),
):

    # Sync with Deck if needed
    deck_id = existing_card.get("deck_id")
    if deck_id:
        await _verify_deck_ownership(deck_id, existing_card.get("user_id"))
        await d_collection.update_one(
            {"_id": ObjectId(deck_id)},
            {"$inc": {"total_cards": -1}, "$pull": {"cards": ObjectId(id)}},
        )

    now = datetime.now(timezone.utc)
    user_id = existing_card.get("user_id")
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now,
        }
    }
    await collection.update_one(
        {"_id": ObjectId(existing_card["_id"])},
        soft_delete_update,
    )
    return None


@router.post("/{id}/review", summary="Review a card with SM-2 grading")
async def review_card(
    id: str,
    grade: str = Query(..., pattern="^(again|hard|good|easy)$"),
    collection: Collection = Depends(get_cards_collection),
    card: dict = Depends(require_ownership(get_cards_collection, "id")),
    user: dict = Depends(get_firebase_user),
):
    """
    Review a card and update its SM-2 spaced repetition parameters.

    - **grade**: User's self-assessment (again, hard, good, easy)
    """
    try:
        from app.utils.sm2 import calculate_next_review
        from app.routers.agent import grant_xp

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

        # Award XP for reviewing a card (fire-and-forget)
        user_id = user.get("user_id")
        await grant_xp(user_id, 2)

        logger.info(f"Successfully updated card {id}")

        return {"message": "Card reviewed successfully", "sm2_data": sm2_result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reviewing card: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reviewing card: {str(e)}")

