from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from pymongo.collection import Collection
from app.models.StudyCard import StudyCard, StudyCardUpdate
from app.models.deck_config import resolve_deck_budget
from app.config.database import cards_collection, decks_collection, books_collection, study_sessions_collection
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
    narrow: Optional[dict] = None,
) -> tuple[list, list]:
    """Pick the new + review cards for a study session, locking today's plan.

    `narrow` (STUDY-001, ADR-014 point 1 server-side) restricts the POOL the
    budgets draw from — `{"tags": {"$in": [...]}}` or `{"_id": {"$in": [...]}}`
    — so a narrowed session is still SM-2's own selection for those cards. It
    is applied to the sticky-pool lookups and the fresh top-up alike, so a
    capped session never introduces a new card the uncapped one would not.

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
    base_match = {"user_id": user_id, "deleted_at": None, "$or": deck_or, **(narrow or {})}

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
            **(narrow or {}),
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


# ---------------------------------------------------------------------------
# STUDY-001 — groups, forecast and narrowed sessions (docs/prd-study-center.md,
# docs/architecture.md addendum)
# ---------------------------------------------------------------------------

STRUGGLING_WINDOW_DAYS = 14
STRUGGLING_GRADES = ("again", "hard")
GROUP_KEYS = ("marked", "struggling")


async def _active_deck_or(user_id: str) -> list:
    """The `$or` clause that scopes a card query to the user's active decks
    (and orphans). Mirrors the inline version in list_study_cards / get_card_tags
    so every count on the study centre agrees with the list it summarises.
    perf(33): the 500-deck cap bounds one user's own deck list, not a fan-out."""
    active_decks = await decks_collection.find(
        {"user_id": user_id, "deleted_at": None}, {"_id": 1}
    ).to_list(length=500)
    active_deck_ids: list = []
    for d in active_decks:
        active_deck_ids.append(d["_id"])
        active_deck_ids.append(str(d["_id"]))
    return [
        {"deck_id": None},
        {"deck_id": {"$exists": False}},
        {"deck_id": {"$in": active_deck_ids}},
    ]


async def _struggling_cards(user_id: str, now_dt: datetime) -> dict:
    """card_id (str) -> {"last_grade", "last_graded_at"} for every card graded
    `again` or `hard` in a logged session within the window (PRD D8). Grades
    live on study_sessions.cards, not on the card, so the answer is a session
    aggregation; the window bounds it to one user's recent sessions (≤ 2000
    distinct cards, which is a fortnight of heavy use)."""
    since = now_dt - timedelta(days=STRUGGLING_WINDOW_DAYS)
    pipeline = [
        {"$match": {"user_id": user_id, "deleted_at": None, "completed_at": {"$gte": since}}},
        {"$sort": {"completed_at": -1}},
        {"$unwind": "$cards"},
        {"$match": {"cards.grade": {"$in": list(STRUGGLING_GRADES)}}},
        {"$group": {
            "_id": "$cards.card_id",
            "last_grade": {"$first": "$cards.grade"},
            "last_graded_at": {"$first": "$completed_at"},
        }},
        {"$limit": 2000},
    ]
    rows = await study_sessions_collection.aggregate(pipeline).to_list(length=2000)
    return {
        str(row["_id"]): {"last_grade": row["last_grade"], "last_graded_at": row["last_graded_at"]}
        for row in rows
        if row.get("_id")
    }


def _object_ids(ids) -> list:
    return [ObjectId(i) for i in ids if ObjectId.is_valid(str(i))]


def _group_stage(now_dt: datetime, key_expr) -> dict:
    """One $group that yields cards / decks / due / new for a set of cards.
    due = reviewed before and next_review ≤ now; new = never reviewed. The two
    never overlap, so the study centre can add them for "Study · N"."""
    return {"$group": {
        "_id": key_expr,
        "cards": {"$sum": 1},
        "deck_ids": {"$addToSet": "$deck_id"},
        "due": {"$sum": {"$cond": [
            {"$and": [{"$ne": ["$last_reviewed", None]}, {"$lte": ["$next_review", now_dt]}]}, 1, 0]}},
        "new": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$last_reviewed", None]}, None]}, 1, 0]}},
    }}


def _summary_row(row: dict) -> dict:
    decks = sorted({str(d) for d in row.get("deck_ids", []) if d is not None})
    # `deck_ids` lets the study centre list a group's decks as rows without a
    # second query; capped so a group over many decks stays a small payload.
    return {"cards": row.get("cards", 0), "decks": len(decks), "deck_ids": decks[:50], "due": row.get("due", 0), "new": row.get("new", 0)}


def _local_day_bounds_utc(tz_name: str, days: int) -> list:
    """UTC boundaries [tomorrow_start, +1d, …] for `days` local days starting
    tomorrow, as naive UTC datetimes (the collection stores naive UTC). Today is
    deliberately absent — it is summary.due_today in /statistics, one owner."""
    try:
        user_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        raise HTTPException(status_code=400, detail=f"Invalid timezone: {tz_name!r}")
    today_local = datetime.now(tz=user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    bounds = []
    for i in range(1, days + 2):
        local = today_local + timedelta(days=i)
        bounds.append(local.astimezone(timezone.utc).replace(tzinfo=None))
    return bounds


@router.get("/forecast", summary="Due counts per local day for the coming days (STUDY-001)")
async def get_forecast(
    days: int = Query(7, ge=1, le=30),
    tz: str = Query("UTC", description="IANA timezone name, e.g. America/New_York"),
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
) -> dict:
    user_id = user.get("user_id")
    bounds = _local_day_bounds_utc(tz, days)
    pipeline = [
        {"$match": {
            "user_id": user_id,
            "deleted_at": None,
            "last_reviewed": {"$ne": None},
            "next_review": {"$gte": bounds[0], "$lt": bounds[-1]},
            "$or": await _active_deck_or(user_id),
        }},
        {"$bucket": {"groupBy": "$next_review", "boundaries": bounds, "default": "other", "output": {"due": {"$sum": 1}}}},
    ]
    rows = await collection.aggregate(pipeline).to_list(length=days + 1)
    by_bound = {row["_id"]: row["due"] for row in rows if row["_id"] != "other"}
    user_tz = ZoneInfo(tz)
    out = []
    for i in range(days):
        local_date = (bounds[i].replace(tzinfo=timezone.utc).astimezone(user_tz)).date().isoformat()
        out.append({"date": local_date, "due": by_bound.get(bounds[i], 0)})
    return {"days": out, "total": sum(d["due"] for d in out)}


@router.get("/groups", summary="Tags and system groups with due and new counts (STUDY-001)")
async def get_groups(
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
) -> dict:
    user_id = user.get("user_id")
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    scope = {"user_id": user_id, "deleted_at": None, "$or": await _active_deck_or(user_id)}

    tag_rows = await collection.aggregate([
        {"$match": {**scope, "tags": {"$exists": True, "$ne": []}}},
        {"$unwind": "$tags"},
        _group_stage(now_dt, "$tags"),
        {"$sort": {"due": -1, "cards": -1, "_id": 1}},
        {"$limit": 500},
    ]).to_list(length=500)
    tags = [{"tag": row["_id"], **_summary_row(row)} for row in tag_rows]

    async def summarise(narrow: dict) -> dict:
        rows = await collection.aggregate([
            {"$match": {**scope, **narrow}},
            _group_stage(now_dt, None),
        ]).to_list(length=1)
        return _summary_row(rows[0]) if rows else {"cards": 0, "decks": 0, "deck_ids": [], "due": 0, "new": 0}

    marked = await summarise({"marked_at": {"$ne": None}})
    struggling_ids = _object_ids((await _struggling_cards(user_id, now_dt)).keys())
    struggling = await summarise({"_id": {"$in": struggling_ids}}) if struggling_ids else {"cards": 0, "decks": 0, "deck_ids": [], "due": 0, "new": 0}

    return {
        "system": [
            {"key": "marked", **marked},
            {"key": "struggling", **struggling, "window_days": STRUGGLING_WINDOW_DAYS},
        ],
        "tags": tags,
    }


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

        # Weekly progress + streak are computed server-side via an aggregation
        # instead of pulling up to 2000 raw card docs into a Python loop
        # (PERF-01 / D-01 finding #1). $match is copied VERBATIM from the
        # replaced .find() filter (user_id, deleted_at, last_reviewed) as the
        # pipeline's FIRST stage — the sole BOLA enforcement point for this
        # aggregation (T-33-01). $group buckets by (day, type) so weekly
        # buckets and the streak can both be derived from pre-grouped counts.
        # No `timezone` param on $dateToString — the Motor client stores
        # naive-UTC datetimes (no tz_aware), so the default UTC formatting
        # matches the day boundaries already used throughout this file
        # (A2 spot-check: a review at 2026-07-30T00:00:00 UTC groups into
        # "2026-07-30", the same calendar day datetime.now(utc).replace(tzinfo=None)
        # would bucket it into).
        from datetime import datetime, timedelta, timezone

        ninety_days_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
        weekly_pipeline = [
            {"$match": {
                "user_id": user_id,
                "deleted_at": None,
                "last_reviewed": {"$gte": ninety_days_ago},
            }},
            {"$group": {
                "_id": {
                    "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$last_reviewed"}},
                    "type": {"$ifNull": ["$card_type", "flashcard"]},
                },
                "count": {"$sum": 1},
            }},
        ]
        # Bounded: 90 days * at most 3 card types per day.
        grouped_counts = await collection.aggregate(weekly_pipeline).to_list(length=700)

        day_type_totals: dict = {}
        for row in grouped_counts:
            day = row["_id"]["day"]
            card_type = row["_id"]["type"]
            day_type_totals.setdefault(day, {})[card_type] = row.get("count", 0)

        # books_collection is a per-user own-book fetch (bounded, cheap at
        # current scale) — left unchanged, only hoisted to a module-level
        # import so it is patchable in tests.
        all_books = await books_collection.find({"user_id": user_id, "deleted_at": None}).to_list(length=500)

        # Calculate weekly progress (last 7 days) - separated by type
        today = datetime.now(timezone.utc).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_data = []

        for i in range(6, -1, -1):  # Last 7 days (6 days ago to today)
            day_start = today - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            date_str = day_start.strftime("%Y-%m-%d")
            type_counts = day_type_totals.get(date_str, {})

            flashcards_count = type_counts.get("flashcard", 0)
            quizzes_count = type_counts.get("quiz", 0)
            visual_count = type_counts.get("visual", 0)

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
                    "date": date_str,
                    "cards": total_count,  # Keep for backwards compatibility
                    "flashcards": flashcards_count,
                    "quizzes": quizzes_count,
                    "visual": visual_count,
                    "books": books_count,
                }
            )

        # Recent performance stays its OWN small bounded query (needs record
        # fields the grouped counts don't carry: title/ease_factor/type) —
        # not folded into the $group above (RESEARCH.md recommends against
        # over-engineering a single giant $facet).
        recent_cards = await collection.find(
            {
                "user_id": user_id,
                "deleted_at": None,
                "last_reviewed": {"$ne": None},
            }
        ).sort("last_reviewed", -1).limit(10).to_list(length=10)

        recent_performance = []
        for card in recent_cards:
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

        # Current streak (consecutive days ending today with >=1 review),
        # derived from the same grouped day/type counts computed above
        # instead of re-scanning raw docs. Naturally bounded to the same
        # 90-day window as the aggregation's $match (last_reviewed >=
        # ninety_days_ago), matching the prior Python-loop's implicit cap.
        reviewed_days = set(day_type_totals.keys())
        streak = 0
        check_date = today
        while True:
            if check_date.strftime("%Y-%m-%d") in reviewed_days:
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
    # perf(33): 500-deck cap — bounds this user's OWN active-deck list (one
    # doc per deck), not a per-deck card fan-out; a single user's deck count
    # stays far below 500 at this app's current scale. Retained as-is, not
    # lowered — truncating would silently drop a legitimate power user's own
    # decks (D-01 finding #2).
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
    limit: Optional[int] = Query(None, ge=1, le=500, description="Cap the session; due cards first (Quick 10)"),
    tags: Optional[List[str]] = Query(None, description="Narrow the pool to cards carrying any of these tags"),
    group: Optional[str] = Query(None, pattern="^(struggling)$", description="Only a group the scheduler may read (ADR-014 point 2); anything else is a 422"),
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
) -> dict:
    """Aggregate today's session across every active deck.

    `tags` / `group` narrow the pool before the per-deck budgets apply
    (STUDY-001, ADR-014 point 1). The `group` pattern admits only groups the
    scheduler may read, so an axis it must not see (ADR-014 point 2, ADR-010)
    never reaches this function — validation refuses it first. `limit` slices
    the selection already made — due cards first, then new — so a capped
    session never introduces more new cards than the full one.

    Each deck contributes its own locked new-card pool (capped at the deck's
    `new_per_day`) and its own remaining review budget. The selection is sticky
    for the day via `introduced_at` — the same cards reappear across sessions
    until they are graded.
    """
    user_id = user.get("user_id")
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    narrow: dict = {}
    if tags:
        narrow["tags"] = {"$in": tags}
    if group == "struggling":
        narrow["_id"] = {"$in": _object_ids((await _struggling_cards(user_id, now_dt)).keys())}

    # perf(33): 500-deck cap — bounds this user's OWN active-deck list (one
    # doc per deck), not a per-deck card fan-out; a single user's deck count
    # stays far below 500 at this app's current scale. Retained as-is, not
    # lowered — truncating would silently drop a legitimate power user's own
    # decks (D-01 finding #2).
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
            narrow=narrow or None,
        )
        all_new.extend(new_raw)
        all_review.extend(review_raw)

    if limit is not None and len(all_new) + len(all_review) > limit:
        # Due first, then new, on the pool already selected and stamped above.
        keep_review = all_review[:limit]
        keep_new = all_new[: max(0, limit - len(keep_review))]
        all_review, all_new = keep_review, keep_new

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
    marked_only: bool = Query(False),
    group: Optional[str] = Query(None, pattern="^(marked|struggling)$"),
    collection: Collection = Depends(get_cards_collection),
    user: dict = Depends(get_firebase_user),
) -> dict:
    user_id = user.get("user_id")
    logger.info(f"Listing study cards for user: {user_id}")
    # STUDY-001: a system group is a narrowing like `tags`. Struggling carries
    # its grades back onto the cards it returns (PRD FR-003).
    struggling: dict = {}
    if group == "marked":
        marked_only = True
    elif group == "struggling":
        struggling = await _struggling_cards(user_id, datetime.now(timezone.utc).replace(tzinfo=None))

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
        # perf(33): 500-deck cap — bounds this user's OWN active-deck list (one
        # doc per deck), not a per-deck card fan-out; a single user's deck count
        # stays far below 500 at this app's current scale. Retained as-is, not
        # lowered — truncating would silently drop a legitimate power user's own
        # decks (D-01 finding #2).
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

    if group == "struggling":
        query["_id"] = {"$in": _object_ids(struggling.keys())}

    if marked_only:
        # A plain top-level key by design: the deck, search and due clauses all
        # rewrite `$or`/`$and` below, and this must survive every one of them
        # rather than competing for the same key. `$ne: None` also excludes the
        # documents that predate the field, which Mongo treats as null.
        query["marked_at"] = {"$ne": None}

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
        if struggling and c["_id"] in struggling:
            c["last_grade"] = struggling[c["_id"]]["last_grade"]
            c["last_graded_at"] = struggling[c["_id"]]["last_graded_at"]

    return {
        "cards": cards,
        "total": total,
        "page": (skip // limit) + 1 if limit > 0 else 1,
        "has_more": skip + len(cards) < total,
    }


@router.patch("/{id}", summary="Update a study card", response_model=StudyCard)
async def update_study_card(
    id: str,
    payload: StudyCardUpdate,
    collection: Collection = Depends(get_cards_collection),
    d_collection: Collection = Depends(get_decks_collection),
    existing_card: dict = Depends(require_ownership(get_cards_collection, "id")),
):
    """Edit a card's content. Scheduling and the user mark are not editable here.

    `StudyCardUpdate` is an allowlist with `extra="forbid"`, so a field this
    route does not own is refused by validation before any of this runs — see
    that model for why it is an allowlist and not a denylist (DEBT-002).

    `exclude_unset=True` is what keeps this a genuine PATCH: an omitted field is
    untouched, while an explicitly-sent `null` still clears the value. That
    distinction is load-bearing for `deck_id`, where `null` means "remove this
    card from its deck".
    """
    updates = payload.model_dump(exclude_unset=True)

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

    # Nothing to do — a PATCH with no recognised field is a no-op, not an error.
    if not updates:
        existing_card["_id"] = str(existing_card["_id"])
        return existing_card

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


async def _write_mark(
    id: str, collection: Collection, marked_at: Optional[datetime]
) -> dict:
    """Set or clear `marked_at` on one card, and touch nothing else.

    The narrowness is the guarantee, not an implementation detail: this writes
    exactly two keys, so no future edit can make the user's mark move the
    scheduler without visibly widening this function (ADR-010). Ownership is
    already enforced by the `require_ownership` dependency on both callers.
    """
    await collection.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "marked_at": marked_at,
                "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        },
    )

    card = await collection.find_one({"_id": ObjectId(id)})
    card["_id"] = str(card["_id"])
    if card.get("deck_id"):
        card["deck_id"] = str(card["deck_id"])
    if card.get("user_id"):
        card["user_id"] = str(card["user_id"])
    return card


@router.put("/{id}/mark", summary="Mark a card for later review", response_model=StudyCard)
async def mark_study_card(
    id: str,
    collection: Collection = Depends(get_cards_collection),
    existing_card: dict = Depends(require_ownership(get_cards_collection, "id")),
):
    """Flag a card as one the user wants to come back to.

    This is the user's own axis and is deliberately invisible to SM-2: it does
    not grade the card, does not move `next_review`, and is never read when a
    study session is assembled. Its destination is the marked filter in free
    study (Browse mode), where reviews are refused anyway — so drilling a marked
    card can never inflate its ease. See ADR-010.

    Idempotent: marking an already-marked card refreshes the timestamp rather
    than erroring, so a double-tap is harmless.
    """
    logger.info(f"Marking card {id}")
    return await _write_mark(
        id, collection, datetime.now(timezone.utc).replace(tzinfo=None)
    )


@router.delete("/{id}/mark", summary="Clear a card's mark", response_model=StudyCard)
async def unmark_study_card(
    id: str,
    collection: Collection = Depends(get_cards_collection),
    existing_card: dict = Depends(require_ownership(get_cards_collection, "id")),
):
    """Clear the user's mark.

    Only the user clears a mark — nothing in the app does it for them (A5). That
    is why this has to be reachable in one action from inside the session, and
    why it is idempotent: unmarking an unmarked card succeeds.
    """
    logger.info(f"Unmarking card {id}")
    return await _write_mark(id, collection, None)


@router.post("/{id}/review", summary="Review a card with SM-2 grading")
async def review_card(
    id: str,
    grade: str = Query(..., pattern="^(again|hard|good|easy)$"),
    mode: str = Query("study", pattern="^(study|browse)$"),
    collection: Collection = Depends(get_cards_collection),
    card: dict = Depends(require_ownership(get_cards_collection, "id")),
    user: dict = Depends(get_firebase_user),
):
    """
    Review a card and update its SM-2 spaced repetition parameters.

    - **grade**: User's self-assessment (again, hard, good, easy)
    - **mode**: Active session mode (study, browse). Only `study` may
      grade a card and mutate its SM-2 schedule; `browse` is rejected
      with 403 before any write.
    """
    if mode != "study":
        raise HTTPException(
            status_code=403,
            detail="Reviews cannot be graded in Browse mode.",
        )

    try:
        from app.utils.sm2 import calculate_next_review
        from app.routers.agent import grant_xp, XP_PER_CARD_REVIEW

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

        # Award XP for reviewing a card — genuinely fire-and-forget: the SM-2
        # update above already committed, so a grant_xp failure must never
        # turn an already-persisted review into a client-facing 500 (which
        # would cause the frontend's retry queue to resubmit and re-apply
        # the same grade a second time — see 32-REVIEW.md CR-01).
        #
        # The result IS returned now (PET-004). It was previously computed and
        # discarded, which meant the most common way to level up — reviewing a
        # card — was also the only way the user was never told about it. On
        # failure the xp block stays None and the client keeps its last known
        # progress rather than snapping a progress bar to zero.
        user_id = user.get("user_id")
        xp_result: Optional[dict] = None
        try:
            xp_result = await grant_xp(user_id, XP_PER_CARD_REVIEW)
        except Exception as xp_err:
            logger.warning(
                f"grant_xp failed for user {user_id} after review of card {id}: {xp_err}"
            )

        logger.info(f"Successfully updated card {id}")

        return {
            "message": "Card reviewed successfully",
            "sm2_data": sm2_result,
            "xp": {
                "xp_awarded": XP_PER_CARD_REVIEW,
                "level_up": xp_result["level_up"],
                "new_level": xp_result["new_level"],
                "new_stage": xp_result["new_stage"],
                "avatar_regen_pending": xp_result.get("avatar_regen_pending", False),
                "current_xp": xp_result.get("current_xp"),
                "xp_for_next_level": xp_result.get("xp_for_next_level"),
                "level_progress": xp_result.get("level_progress"),
            }
            if xp_result
            else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reviewing card: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reviewing card: {str(e)}")

