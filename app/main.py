import os
from dotenv import load_dotenv

# Load env before importing other modules that rely on env vars
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.config.database import create_indexes
from app.core.limiter import limiter
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
)




@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await create_indexes()
    yield
    # Shutdown (if needed)


app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
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
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Only methods used by API
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


@app.get("/")
async def root():
    return {"status": "ok", "message": "Nowry API is running"}
