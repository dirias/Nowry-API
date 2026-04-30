import logging
import os
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

# --- Load MongoDB URI and DB name from environment ---
MONGO_DB = os.getenv("MONGO_DB", "mydb")

# Use single URI for both Local (Docker) and Production
MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:example@mongodb:27017/")

# --- Create client ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB]

# --- Collections ---
users_collection = db["users"]
books_collection = db["books"]
decks_collection = db["decks"]
cards_collection = db["cards"]
study_cards_collection = db["cards"]  # Alias for cards collection
tasks_collection = db["tasks"]
bugs_collection = db["bugs"]

# --- Annual Planning Collections ---
annual_plans_collection = db["annual_plans"]
focus_areas_collection = db["focus_areas"]
priorities_collection = db["priorities"]
goals_collection = db["goals"]
activities_collection = db["activities"]
daily_routines_collection = db["daily_routines"]
quarter_reports_collection = db["quarter_reports"]
book_chunks_collection = db["book_chunks"]
quiz_sessions_collection = db["quiz_sessions"]
# AI quiz — temporary question cache (TTL 24 h); separate from deck quiz_sessions
ai_quiz_sessions_collection = db["ai_quiz_sessions"]
# Permanent session history — never expires, indexed for analytics
study_sessions_collection = db["study_sessions"]

async def create_indexes():
    # User indexes
    await users_collection.create_index("firebase_uid", unique=True)
    await users_collection.create_index("email", unique=True)
    await users_collection.create_index("username", unique=True)

    # Data indexes for performance
    await books_collection.create_index("user_id")
    await books_collection.create_index("created_at")
    await decks_collection.create_index("user_id")
    await decks_collection.create_index("created_at")
    await cards_collection.create_index("deck_id")
    await cards_collection.create_index("user_id")
    await cards_collection.create_index("next_review_date")
    await tasks_collection.create_index("user_id")
    await tasks_collection.create_index("status")

    # Content reports (moderation): status, created_at
    await db["content_reports"].create_index("status")
    await db["content_reports"].create_index("created_at")

    # Annual Planning indexes
    await annual_plans_collection.create_index("user_id")
    await annual_plans_collection.create_index([("user_id", 1), ("year", 1)], unique=True)
    await focus_areas_collection.create_index("annual_plan_id")
    await priorities_collection.create_index("focus_area_id")
    await goals_collection.create_index("focus_area_id")
    await activities_collection.create_index("goal_id")
    await daily_routines_collection.create_index("user_id", unique=True)
    await quarter_reports_collection.create_index("annual_plan_id")

    # Quiz sessions: TTL index — auto-expire sessions after 1 hour.
    # create_index() is idempotent but WON'T update expireAfterSeconds on an
    # existing index. Use collMod to update it if it already exists.
    _QUIZ_TTL_SECONDS = 3600
    existing_indexes = await quiz_sessions_collection.index_information()
    if "quiz_sessions_ttl" in existing_indexes:
        current_ttl = existing_indexes["quiz_sessions_ttl"].get("expireAfterSeconds")
        if current_ttl != _QUIZ_TTL_SECONDS:
            await db.command(
                "collMod",
                "quiz_sessions",
                index={"name": "quiz_sessions_ttl", "expireAfterSeconds": _QUIZ_TTL_SECONDS},
            )
    else:
        await quiz_sessions_collection.create_index(
            "created_at",
            expireAfterSeconds=_QUIZ_TTL_SECONDS,
            name="quiz_sessions_ttl",
        )
    await quiz_sessions_collection.create_index("session_id", unique=True)
    await quiz_sessions_collection.create_index("user_id")
    # Compound index for ownership-scoped session lookups (prevents cross-user access)
    await quiz_sessions_collection.create_index(
        [("session_id", 1), ("user_id", 1)],
        name="quiz_sessions_ownership",
    )

    # AI quiz sessions: TTL 24 hours — stores pre-generated question sets server-side
    _AI_QUIZ_TTL_SECONDS = 86400  # 24 h
    ai_existing_indexes = await ai_quiz_sessions_collection.index_information()
    if "ai_quiz_sessions_ttl" in ai_existing_indexes:
        current_ai_ttl = ai_existing_indexes["ai_quiz_sessions_ttl"].get("expireAfterSeconds")
        if current_ai_ttl != _AI_QUIZ_TTL_SECONDS:
            await db.command(
                "collMod",
                "ai_quiz_sessions",
                index={"name": "ai_quiz_sessions_ttl", "expireAfterSeconds": _AI_QUIZ_TTL_SECONDS},
            )
    else:
        await ai_quiz_sessions_collection.create_index(
            "created_at",
            expireAfterSeconds=_AI_QUIZ_TTL_SECONDS,
            name="ai_quiz_sessions_ttl",
        )
    await ai_quiz_sessions_collection.create_index("session_id", unique=True)
    await ai_quiz_sessions_collection.create_index("user_id")
    await ai_quiz_sessions_collection.create_index(
        [("session_id", 1), ("user_id", 1)],
        name="ai_quiz_sessions_ownership",
    )

    # study_sessions: permanent history, indexed for analytics queries
    await study_sessions_collection.create_index("user_id")
    await study_sessions_collection.create_index("completed_at")
    await study_sessions_collection.create_index(
        [("user_id", 1), ("completed_at", -1)],
        name="study_sessions_user_history",
    )
    await study_sessions_collection.create_index("session_type")

    logger.info("Database indexes created successfully.")
