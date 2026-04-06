from __future__ import annotations

import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.collection import Collection

from app.auth.firebase_auth import get_firebase_user, optional_auth
from app.config.database import cards_collection, decks_collection
from app.models.Deck import Deck, DeckWithStats
from app.models.deck_settings import (
    DeckSettingsUpdate,
    DeckSettingsResponse,
    VoiceSettings,
    StudyConfig,
    PublicMetadataInput,
)
from app.models.deck_config import (
    PACE_DEFAULTS,
    DailyBudgetResponse,
    DeckConfigResponse,
    DeckConfigUpdate,
    PaceMode,
)
from app.utils.logger import get_logger

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


@router.get("", summary="List all decks", response_model=List[DeckWithStats])
async def list_decks(
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    user_id = user.get("user_id")
    logger.info(f"Listing decks for user: {user_id}")

    # Filter by user_id to ensure users only see their own decks
    cursor = collection.find({"user_id": user_id, "deleted_at": None})
    decks = await cursor.to_list(length=100)

    now_dt = datetime.utcnow()

    for d in decks:
        d["_id"] = str(d["_id"])
        if d.get("user_id"):
            d["user_id"] = str(d["user_id"])
        if d.get("cards"):
            d["cards"] = [str(c) for c in d["cards"]]
        
        # Calculate real-time counts
        deck_id_str = d["_id"]

        # Initialize defaults
        total_count = d.get("total_cards", 0)
        due_count = 0
        new_count = 0
        mastery = 0
        last_studied = None
        is_due_soon = False
        
        try:
            deck_oid = ObjectId(deck_id_str)
            # $in matches both ObjectId and string-stored deck_ids (mixed legacy data)
            deck_filter = {"deck_id": {"$in": [deck_oid, deck_id_str]}}

            total_count = await cards_collection.count_documents(
                {**deck_filter, "deleted_at": None}
            )
            # due_count = only cards that have been reviewed before AND whose
            # next_review is now past due.  New cards (last_reviewed is None)
            # are intentionally excluded here — they are counted in new_count.
            due_count = await cards_collection.count_documents({
                **deck_filter,
                "deleted_at": None,
                "last_reviewed": {"$ne": None},
                "next_review": {"$lte": now_dt}
            })
            new_count = await cards_collection.count_documents(
                {**deck_filter, "last_reviewed": None, "deleted_at": None}
            )

            last_card = await cards_collection.find_one(
                {**deck_filter, "last_reviewed": {"$ne": None}, "deleted_at": None},
                sort=[("last_reviewed", -1)]
            )
            last_studied = last_card.get("last_reviewed") if last_card else None

            # Find the next card due in the future to compute exact hours remaining
            next_card = await cards_collection.find_one(
                {**deck_filter, "deleted_at": None, "next_review": {"$gt": now_dt}},
                sort=[("next_review", 1)]
            )
            if next_card and next_card.get("next_review"):
                delta = next_card["next_review"] - now_dt
                hours_until_due = max(1, round(delta.total_seconds() / 3600))
                is_due_soon = hours_until_due <= 24
            else:
                hours_until_due = None
                is_due_soon = False

            # Mastery = avg ease_factor of reviewed cards, normalized 1.3–2.5 → 0–100%
            # Uses last_reviewed (not repetitions — SM-2 resets repetitions on "again")
            pipeline = [
                {"$match": {**deck_filter, "last_reviewed": {"$ne": None}, "deleted_at": None}},
                {"$group": {"_id": None, "avg_ease": {"$avg": "$ease_factor"}}}
            ]
            agg_result = await cards_collection.aggregate(pipeline).to_list(length=1)
            if agg_result:
                avg_ease = agg_result[0].get("avg_ease", 2.5)
                mastery = round(((avg_ease - 1.3) / (2.5 - 1.3)) * 100)

        except Exception as e:
            logger.error(f"Error calculating stats for deck {deck_id_str}: {e}", exc_info=True)

        # Cap raw counts against the deck's configured daily limits so that
        # the frontend always shows "cards available today" not "all-time totals".
        _, new_per_day, max_reviews_per_day = _resolve_deck_config(d)

        # To make the dash counter decrement (e.g. 20 -> 19), we must subtract
        # how many cards were already studied today.
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # New cards studied today (first time they've ever been reviewed)
        new_studied_today = await cards_collection.count_documents({
            **deck_filter,
            "last_reviewed": {"$gte": today_start},
            "repetitions": {"$lte": 1} # Includes fails (0) and first-time success (1)
        })
        
        # Reviews completed today (already had a history before today)
        reviews_done_today = await cards_collection.count_documents({
            **deck_filter,
            "last_reviewed": {"$gte": today_start},
            "repetitions": {"$gt": 1}
        })

        due_count = min(due_count, max(0, max_reviews_per_day - reviews_done_today))
        new_count = min(new_count, max(0, new_per_day - new_studied_today))

        d["total_cards"] = total_count
        d["due_cards"] = due_count
        d["new_cards"] = new_count
        d["mastery"] = mastery
        d["last_studied"] = last_studied
        d["is_due_soon"] = is_due_soon
        d["hours_until_due"] = hours_until_due

    return decks


@router.get("/{id}", summary="Get deck by ID", response_model=Deck)
async def get_deck(
    id: str,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
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
    deck_oid = existing_deck["_id"]
    await cards_collection.update_many(
        {
            "$or": [
                {"deck_id": deck_oid},
                {"deck_id": str(deck_oid)}
            ],
            "deleted_at": None
        },
        soft_delete_update
    )

    return None


# ---------------------------------------------------------------------------
# Helper — build the deck filter that tolerates ObjectId / string deck_ids
# ---------------------------------------------------------------------------

def _resolve_deck_config(deck_doc: dict) -> tuple[PaceMode, int, int]:
    """Extract (pace_mode, new_per_day, max_reviews_per_day) from a deck document,
    falling back to the balanced defaults when no config subdocument is present."""
    cfg = deck_doc.get("config") or {}
    mode_str: str = cfg.get("pace_mode", PaceMode.balanced.value)
    # Guard against a stored value that no longer matches the enum
    try:
        mode = PaceMode(mode_str)
    except ValueError:
        mode = PaceMode.balanced
    defaults = PACE_DEFAULTS[mode.value]
    new_per_day: int = cfg.get("new_per_day", defaults["new_per_day"])
    max_reviews: int = cfg.get("max_reviews_per_day", defaults["max_reviews_per_day"])
    return mode, new_per_day, max_reviews


async def _fetch_owned_deck(deck_id: str, user_id: str) -> dict:
    """Fetch a deck by ID, raise 404 if missing, 403 if not owned."""
    try:
        deck_oid = ObjectId(deck_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Deck not found")

    deck = await decks_collection.find_one({"_id": deck_oid, "deleted_at": None})
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    if str(deck.get("user_id")) != str(user_id):
        raise HTTPException(status_code=404, detail="Deck not found")
    return deck


async def _compute_daily_counts(
    deck_oid: ObjectId,
    new_per_day: int,
    max_reviews: int,
    today_start: datetime,
    today_end: datetime,
) -> tuple[int, int, int, bool]:
    """Return (new_cards_today, reviews_today, total_today, budget_reached)."""
    deck_filter = {"deck_id": {"$in": [deck_oid, str(deck_oid)]}, "deleted_at": None}

    # Unseen = no last_reviewed entry
    raw_unseen: int = await cards_collection.count_documents(
        {**deck_filter, "last_reviewed": None}
    )
    # Due = next_review <= today_end (covers overdue + due today)
    raw_due: int = await cards_collection.count_documents(
        {
            **deck_filter,
            "last_reviewed": {"$ne": None},
            "$or": [
                {"next_review": {"$exists": False}},
                {"next_review": None},
                {"next_review": {"$lte": today_end}},
            ],
        }
    )

    new_cards_today = min(raw_unseen, new_per_day)
    reviews_today = min(raw_due, max_reviews)
    total_today = new_cards_today + reviews_today
    budget_reached = raw_unseen > new_per_day or raw_due > max_reviews
    return new_cards_today, reviews_today, total_today, budget_reached


# ---------------------------------------------------------------------------
# PATCH /decks/{deck_id}/config
# ---------------------------------------------------------------------------

@router.patch(
    "/{deck_id}/config",
    summary="Update study pace configuration for a deck",
    response_model=DeckConfigResponse,
)
async def update_deck_config(
    deck_id: str,
    body: DeckConfigUpdate,
    user: dict = Depends(get_firebase_user),
) -> DeckConfigResponse:
    user_id: str = user.get("user_id")
    deck = await _fetch_owned_deck(deck_id, user_id)
    deck_oid: ObjectId = deck["_id"]

    # Resolve final values — explicit body fields override pace defaults
    defaults = PACE_DEFAULTS[body.pace_mode.value]
    resolved_new: int = body.new_per_day if body.new_per_day is not None else defaults["new_per_day"]
    resolved_max: int = (
        body.max_reviews_per_day
        if body.max_reviews_per_day is not None
        else defaults["max_reviews_per_day"]
    )

    config_doc = {
        "pace_mode": body.pace_mode.value,
        "new_per_day": resolved_new,
        "max_reviews_per_day": resolved_max,
    }

    await decks_collection.update_one(
        {"_id": deck_oid},
        {"$set": {"config": config_doc, "updated_at": datetime.utcnow()}},
    )

    deck_filter = {"deck_id": {"$in": [deck_oid, str(deck_oid)]}, "deleted_at": None}

    total_cards: int = await cards_collection.count_documents(deck_filter)
    introduced_count: int = await cards_collection.count_documents(
        {**deck_filter, "last_reviewed": {"$ne": None}}
    )

    introduced_pct: float = (
        round(introduced_count / total_cards * 100, 1) if total_cards > 0 else 0.0
    )
    remaining = total_cards - introduced_count
    estimated_completion_days: Optional[int] = (
        math.ceil(remaining / resolved_new) if total_cards > 0 and resolved_new > 0 else None
    )

    return DeckConfigResponse(
        deck_id=deck_id,
        pace_mode=body.pace_mode,
        new_per_day=resolved_new,
        max_reviews_per_day=resolved_max,
        introduced_count=introduced_count,
        total_cards=total_cards,
        introduced_pct=introduced_pct,
        estimated_completion_days=estimated_completion_days,
    )


# ---------------------------------------------------------------------------
# GET /decks/{deck_id}/daily-budget
# ---------------------------------------------------------------------------

@router.get(
    "/{deck_id}/daily-budget",
    summary="Get today's new-card and review budget for a deck",
    response_model=DailyBudgetResponse,
)
async def get_daily_budget(
    deck_id: str,
    tz: str = Query("UTC", description="IANA timezone name, e.g. America/New_York"),
    user: dict = Depends(get_firebase_user),
) -> DailyBudgetResponse:
    user_id: str = user.get("user_id")

    # Validate timezone
    try:
        user_tz = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError):
        raise HTTPException(status_code=400, detail=f"Invalid timezone: {tz!r}")

    deck = await _fetch_owned_deck(deck_id, user_id)
    deck_oid: ObjectId = deck["_id"]

    _, new_per_day, max_reviews = _resolve_deck_config(deck)

    # Compute today's UTC midnight boundaries from the user's local date
    now_local = datetime.now(tz=user_tz)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_local = today_local + timedelta(days=1)
    today_start_utc = today_local.astimezone(timezone.utc).replace(tzinfo=None)
    today_end_utc = tomorrow_local.astimezone(timezone.utc).replace(tzinfo=None)

    new_cards_today, reviews_today, total_today, budget_reached = await _compute_daily_counts(
        deck_oid, new_per_day, max_reviews, today_start_utc, today_end_utc
    )

    return DailyBudgetResponse(
        deck_id=deck_id,
        new_cards_today=new_cards_today,
        reviews_today=reviews_today,
        total_today=total_today,
        budget_reached=budget_reached,
        new_per_day_limit=new_per_day,
        reviews_per_day_limit=max_reviews,
    )


# ---------------------------------------------------------------------------
# PATCH /decks/{deck_id}/settings
# ---------------------------------------------------------------------------

@router.patch(
    "/{deck_id}/settings",
    summary="Update deck settings (voice, pace, publishing)",
    response_model=DeckSettingsResponse,
)
async def update_deck_settings(
    deck_id: str,
    body: DeckSettingsUpdate,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
) -> DeckSettingsResponse:
    user_id = user.get("user_id")

    try:
        obj_id = ObjectId(deck_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Deck not found")

    deck = await collection.find_one({"_id": obj_id, "deleted_at": None})
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    if str(deck.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    set_doc: dict = {"updated_at": datetime.utcnow()}

    if body.voice_settings is not None:
        set_doc["voice_settings"] = body.voice_settings.model_dump()

    if body.config is not None:
        set_doc["config"] = body.config.model_dump()

    if body.is_public is not None:
        set_doc["is_public"] = body.is_public
        if body.is_public and not deck.get("published_at"):
            set_doc["published_at"] = datetime.utcnow()

    if body.public_metadata is not None:
        set_doc["public_metadata"] = body.public_metadata.model_dump(exclude_none=True)

    updated = await collection.find_one_and_update(
        {"_id": obj_id},
        {"$set": set_doc},
        return_document=True,
    )

    raw_voice = updated.get("voice_settings") or {}
    raw_config = updated.get("config") or {}

    voice = VoiceSettings.model_validate(raw_voice) if raw_voice else VoiceSettings()
    config = StudyConfig.model_validate(raw_config) if raw_config else StudyConfig()

    pub_meta_raw = updated.get("public_metadata")
    pub_meta = PublicMetadataInput.model_validate(pub_meta_raw) if pub_meta_raw else None

    return DeckSettingsResponse(
        id=str(updated["_id"]),
        voice_settings=voice,
        config=config,
        is_public=updated.get("is_public", False),
        published_at=updated.get("published_at"),
        public_metadata=pub_meta,
        updated_at=updated["updated_at"],
    )
