import os
from dotenv import load_dotenv

# Load env before importing other modules that rely on env vars
load_dotenv()

# Initialize Sentry BEFORE FastAPI app creation — gated on env var presence
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

_SENTRY_DSN = os.getenv("SENTRY_DSN")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        integrations=[
            FastApiIntegration(),
        ],
        traces_sample_rate=0.1 if os.getenv("ENVIRONMENT") == "production" else 1.0,
        environment=os.getenv("ENVIRONMENT", "development"),
        debug=False,
    )

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.config.database import create_indexes
from app.core.limiter import limiter
from app.core import langfuse_client as _langfuse_module

logger = logging.getLogger(__name__)
from app.routers import (
    books,
    users,
    sessions,
    cards,
    study_cards,
    tasks,
    decks,
    quizzes,
    visualizer,
    news,
    bugs,
    annual_planning,
    image_upload,
    auth,
    public_content,
    moderation,
    blackboards,
    import_apkg,
    agent,
    quiz,
    quiz_ai,
    study_sessions,
    stripe_webhooks,
    subscriptions,
    tts,
    illustrations,
    sheets,
    goal_ai,
)




async def _refresh_langfuse_cache() -> None:
    """
    Background task (non-blocking): write an updated langfuse_cache.json timestamp.
    Phase 9 scope: cache skeleton only. Phase 10 populates prompts; Phase 11 syncs both.
    If any error occurs, logs a warning and continues — never raises.
    """
    try:
        logger.info("Refreshing Langfuse cache in background...")
        cache_data = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "prompts": {},
            "model_config": {},
        }
        cache_path = Path(__file__).parent / "config" / "langfuse_cache.json"
        with open(cache_path, "w") as f:
            json.dump(cache_data, f, indent=2)
        logger.info("Langfuse cache refreshed: %s", cache_path)
    except Exception as e:
        logger.warning("Failed to refresh Langfuse cache: %s. Using existing cache if available.", e)


async def _flush_langfuse_queue() -> None:
    """
    Graceful shutdown: flush pending Langfuse traces with 5s timeout.
    Prevents Railway SIGKILL from silently dropping the last trace batch.
    Uses run_in_executor to avoid blocking the event loop during shutdown.
    """
    client = _langfuse_module._langfuse_client
    if not client:
        return
    try:
        logger.info("Flushing Langfuse queue (5s timeout)...")
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, client.flush),
            timeout=5.0,
        )
        logger.info("Langfuse queue flushed successfully.")
    except asyncio.TimeoutError:
        logger.warning("Langfuse queue flush timed out after 5s. Some traces may be lost.")
    except Exception as e:
        logger.warning("Langfuse queue flush failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await create_indexes()
    # [Langfuse] Non-blocking cache refresh — fire task, do NOT await
    # In-flight requests proceed immediately; cache writes in background (D-07)
    if _langfuse_module._langfuse_client:
        asyncio.create_task(_refresh_langfuse_cache())
    yield
    # Shutdown
    await _flush_langfuse_queue()


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Stripe webhook router MUST be registered before any middleware.
# Stripe sends POST /stripe/webhook with a Stripe-Signature header (not a Firebase token).
# Any auth or CORS middleware registered first would reject Stripe's requests with 401.
app.include_router(stripe_webhooks.router)

app.add_middleware(SlowAPIMiddleware)

# CORS Configuration
# Get allowed origins from env or default to localhost
allowed_origins_env = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000",
)
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(auth.router)  # Firebase auth
app.include_router(books.router)
app.include_router(users.router)
app.include_router(sessions.router)
app.include_router(cards.router)
app.include_router(study_cards.router)
app.include_router(decks.router)
app.include_router(tasks.router)
app.include_router(quizzes.router)
app.include_router(visualizer.router)
app.include_router(news.router)
app.include_router(bugs.router)
app.include_router(annual_planning.router)
app.include_router(image_upload.router)
app.include_router(public_content.router)  # Public content sharing
app.include_router(moderation.router)  # Content moderation
app.include_router(blackboards.router)  # Blackboard brainstorm canvas
app.include_router(import_apkg.router)  # Anki .apkg import
app.include_router(agent.router)         # Study Buddy AI companion
app.include_router(quiz.router, prefix="/v1/assistant/quiz", tags=["quiz"])      # Active Study Partner (deck)
app.include_router(quiz_ai.router, prefix="/v1/assistant/quiz", tags=["quiz"])   # Active Study Partner (AI)
app.include_router(study_sessions.router, prefix="/v1/study-sessions", tags=["study-sessions"])  # Session history
app.include_router(subscriptions.router)  # Stripe checkout, portal, and subscription status
app.include_router(tts.router)            # AMagic TTS — POST /book/{book_id}/tts
app.include_router(illustrations.router)  # Illustration Magic — POST /book/{book_id}/diagram
app.include_router(sheets.router)         # Micro Sheets — CRUD /sheets
app.include_router(goal_ai.router)        # Goal AI — POST /goal-ai/analyze (Pro-only)


@app.get("/")
async def root():
    return {"status": "ok", "message": "Nowry API is running"}
