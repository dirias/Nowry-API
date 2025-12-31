import os
from dotenv import load_dotenv

# Load env before importing other modules that rely on env vars
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.config.database import create_indexes
from app.routers import (
    book_pages,
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
)

load_dotenv()

# Initialize Rate Limiter
limiter = Limiter(key_func=get_remote_address)


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
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],
)

app.include_router(book_pages.router)
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
